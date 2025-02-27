# Copyright (c) Meta Platforms, Inc. and affiliates
# Owner(s): ["oncall: distributed"]
import os

import torch
import torch.distributed._functional_collectives as funcol
from torch.distributed._tensor import DTensor
from torch.distributed._tensor._collective_utils import (
    mesh_all_to_all,
    mesh_broadcast,
    mesh_scatter,
)
from torch.distributed._tensor.placement_types import _Partial, Shard
from torch.distributed.device_mesh import _mesh_resources, DeviceMesh, init_device_mesh

from torch.distributed.distributed_c10d import (
    _get_process_group_name,
    _world,
    get_global_rank,
    get_process_group_ranks,
    get_world_size,
    init_process_group,
    is_initialized,
    is_nccl_available,
    new_group,
    ProcessGroup,
)
from torch.testing._internal.common_utils import run_tests
from torch.testing._internal.distributed._tensor.common_dtensor import (
    DTensorTestBase,
    with_comms,
)
from torch.testing._internal.distributed.fake_pg import FakeStore


def _get_device_type(world_size):
    if (
        torch.cuda.is_available()
        and torch.cuda.device_count() >= world_size
        and is_nccl_available()
    ):
        device_type = "cuda"
    else:
        device_type = "cpu"
    return device_type


def _set_env_var(addr="localhost", port="25364", world_size=1, rank=0):
    os.environ["MASTER_ADDR"] = addr
    os.environ["MASTER_PORT"] = port
    os.environ["WORLD_SIZE"] = f"{world_size}"
    os.environ["RANK"] = f"{rank}"


class DeviceMeshTest(DTensorTestBase):
    @property
    def world_size(self):
        return 4

    def test_init_process_group(self):
        device_type = _get_device_type(self.world_size)
        mesh_tensor = torch.arange(4).reshape(2, 2)
        self.assertTrue(not is_initialized())
        _set_env_var(world_size=self.world_size, rank=self.rank)
        DeviceMesh(device_type, mesh_tensor)
        self.assertTrue(is_initialized())
        self.destroy_pg()

    def _get_tags_to_pg_name(self, tags_to_pg):
        tags_to_pg_name = {}
        for tag_name, groups in tags_to_pg.items():
            tags_to_pg_name[tag_name] = []
            for group in groups:
                tags_to_pg_name[tag_name].append(_get_process_group_name(group))
        return tags_to_pg_name

    @with_comms
    def test_reuse_process_group(self):
        # Manually create new_group.
        tp_group_0 = new_group([0, 1])
        tp_group_1 = new_group([2, 3])
        dp_group_0 = new_group([0, 2])
        dp_group_1 = new_group([1, 3])

        # Record the pg tags_to_pg_name so we can check whether init_device_mesh create new pg.
        # We cannot do a deepcopy of ProcessGroup for comparison,
        # since we cannot pickle 'torch._C._distributed_c10d.ProcessGroup' object.
        # Therefore, we rely on the tags_to_pg_name for comparison to make sure
        # before/after init_device_mesh, tags_to_pg in the current world does not change.
        ref_tags_to_pg = _world.tags_to_pg
        ref_tags_to_pg_name = self._get_tags_to_pg_name(ref_tags_to_pg)

        mesh_shape = (2, self.world_size // 2)
        mesh_dim_names = ("DP", "TP")
        mesh_2d = init_device_mesh(
            self.device_type, mesh_shape, mesh_dim_names=mesh_dim_names
        )
        tags_to_pg = _world.tags_to_pg
        tags_to_pg_name = self._get_tags_to_pg_name(tags_to_pg)

        # Check before/after init_device_mesh, tags_to_pg in the current world does not change.
        self.assertEqual(ref_tags_to_pg_name, tags_to_pg_name)

        # For each rank, there should be 4 tags_to_pg, including tags:
        #   1) default tag for user PGs (which contains all the pgs on a particular rank)
        #   2) tag for world size pg
        #   3) tag for tp pg
        #   4) tag for dp pg
        self.assertEqual(len(tags_to_pg), 4)
        # "" is the default tag for user PGs, and the default tag should only
        # contain 3 pgs, since we re-use pgs, whenever possible. The 3 pgs are:
        #   1) pg for the whole world
        #   2) pg for tp_group
        #   3) pg for dp_group
        self.assertEqual(len(tags_to_pg[""]), 3)

        default_user_pgs = tags_to_pg[""]
        dp_dim = 0
        tp_dim = 1
        for idx, group in enumerate(default_user_pgs):
            # world size pg
            if idx == 0:
                self.assertEqual(get_process_group_ranks(group), range(self.world_size))
            # tp pg
            if idx == 1:
                # rank0 - mesh_2d._dim_group_infos is: [('ptd:3', [0, 2]), ('ptd:1', [0, 1])]
                tp_group_ranks = mesh_2d._dim_group_infos[tp_dim][1]
                self.assertEqual(get_process_group_ranks(group), tp_group_ranks)
            # dp pg
            if idx == 2:
                dp_group_ranks = mesh_2d._dim_group_infos[dp_dim][1]
                self.assertEqual(get_process_group_ranks(group), dp_group_ranks)

    @with_comms
    def test_get_group(self):
        mesh_shape = (2, self.world_size // 2)
        mesh_2d = init_device_mesh(
            self.device_type, mesh_shape, mesh_dim_names=("dp", "tp")
        )

        tp_mesh = mesh_2d["tp"]
        dp_mesh = mesh_2d["dp"]

        self.assertEqual(len(mesh_2d.get_group()), 2)
        self.assertEqual(mesh_2d.get_group()[0], mesh_2d.get_group("dp"))
        self.assertEqual(mesh_2d.get_group()[1], mesh_2d.get_group("tp"))

        self.assertEqual(mesh_2d.get_group(0), mesh_2d.get_group("dp"))
        self.assertEqual(mesh_2d.get_group(1), mesh_2d.get_group("tp"))

        self.assertEqual(mesh_2d.get_group("dp"), dp_mesh.get_group())
        self.assertEqual(mesh_2d.get_group("tp"), tp_mesh.get_group())

    @with_comms
    def test_get_local_rank_raises_exception(self):
        mesh_shape = (2, self.world_size // 2)
        mesh_2d = init_device_mesh(
            self.device_type, mesh_shape, mesh_dim_names=("dp", "tp")
        )

        with self.assertRaisesRegex(
            RuntimeError,
            "Optional kwarg `mesh_dim` needs to be specified when device_mesh.ndim > 1.",
        ):
            local_rank = mesh_2d.get_local_rank()

    @with_comms
    def test_get_local_rank(self):
        mesh_shape = (2, self.world_size // 2)
        mesh_2d = init_device_mesh(
            self.device_type, mesh_shape, mesh_dim_names=("dp", "tp")
        )
        self.assertEqual(mesh_2d.get_local_rank("dp"), mesh_2d.get_local_rank(0))
        self.assertEqual(mesh_2d.get_local_rank("tp"), mesh_2d.get_local_rank(1))

        dp_mesh = mesh_2d["dp"]
        tp_mesh = mesh_2d["tp"]
        self.assertEqual(dp_mesh.get_local_rank(), mesh_2d.get_local_rank("dp"))
        self.assertEqual(tp_mesh.get_local_rank(), mesh_2d.get_local_rank("tp"))

    @with_comms
    def test_device_mesh_2d(self):
        mesh_tensor = torch.arange(4).reshape(2, 2)
        # construct a cuda device mesh
        mesh = DeviceMesh(self.device_type, mesh_tensor)

        # check all dim groups
        dim_to_subgroups = mesh.get_group()

        expected_ranks_by_dim = [[[0, 2], [1, 3]], [[0, 1], [2, 3]]]
        for dim, dim_group in enumerate(dim_to_subgroups):
            self.assertTrue(dim < 2)
            dim_ranks = expected_ranks_by_dim[dim]

            dim_group_size = get_world_size(dim_group)
            self.assertIsInstance(dim_group, ProcessGroup)
            self.assertEqual(dim_group_size, 2)
            global_ranks = [
                get_global_rank(dim_group, i) for i in range(dim_group_size)
            ]
            current_rank_expected_group_ranks = (
                dim_ranks[0] if self.rank in dim_ranks[0] else dim_ranks[1]
            )
            self.assertEqual(global_ranks, current_rank_expected_group_ranks)

    def test_fake_pg_device_mesh(self):
        fake_store = FakeStore()
        init_process_group("fake", store=fake_store, rank=0, world_size=self.world_size)
        device_type = "cuda" if torch.cuda.is_available() else "cpu"
        mesh = DeviceMesh(device_type, torch.arange(self.world_size))

        local_tensor = torch.randn(2, 8)
        global_tensor = funcol.all_gather_tensor(
            local_tensor, gather_dim=0, group=(mesh, 0)
        )
        self.assertEqual(global_tensor.shape, (self.world_size * 2, 8))


class DeviceMeshTestNDim(DTensorTestBase):
    @property
    def world_size(self):
        return 8

    @with_comms
    def test_device_mesh_nd(self):
        # construct a cuda device mesh
        mesh_tensor = torch.arange(8).reshape(2, 2, 2)
        mesh = DeviceMesh(self.device_type, mesh_tensor)

        # check all dim groups
        dim_to_subgroups = mesh.get_group()

        for dim, dim_group in enumerate(dim_to_subgroups):
            self.assertTrue(dim < mesh_tensor.ndim)
            dim_ranks = mesh_tensor.swapdims(-1, dim).reshape(-1, 2)

            dim_group_size = get_world_size(dim_group)
            self.assertIsInstance(dim_group, ProcessGroup)
            self.assertEqual(dim_group_size, 2)
            global_ranks = [
                get_global_rank(dim_group, i) for i in range(dim_group_size)
            ]
            for ranks in dim_ranks:
                if self.rank in ranks:
                    self.assertEqual(global_ranks, ranks.tolist())

    @with_comms
    def test_device_mesh_hash(self):
        mesh_tensor_2d = torch.arange(8).reshape(4, 2)
        mesh = DeviceMesh(self.device_type, mesh_tensor_2d)
        mesh2 = DeviceMesh(self.device_type, mesh_tensor_2d)
        self.assertNotEqual(hash(mesh), hash(mesh2))
        mesh_tensor_3d = torch.arange(8).reshape(2, 2, 2)
        mesh3 = DeviceMesh(self.device_type, mesh_tensor_3d)
        self.assertNotEqual(hash(mesh), hash(mesh3))
        self.assertNotEqual(hash(mesh2), hash(mesh3))


class InitDeviceMeshTest(DTensorTestBase):
    @property
    def world_size(self):
        return 8

    @with_comms
    def test_init_device_mesh(self):
        mesh_shape = (2, 4)
        ref_mesh = DeviceMesh(self.device_type, torch.arange(8).view(mesh_shape))

        # test init_device_mesh with mesh_dim_names
        mesh_dim_names = ("DP", "TP")
        mesh_2d = init_device_mesh(
            self.device_type, mesh_shape, mesh_dim_names=mesh_dim_names
        )
        self.assertEqual(mesh_2d, ref_mesh)
        self.assertEqual(mesh_2d.mesh_dim_names, mesh_dim_names)

        # test init_device_mesh without mesh_dim_names
        mesh_2d = init_device_mesh(self.device_type, mesh_shape)
        self.assertEqual(mesh_2d, ref_mesh)

    @with_comms
    def test_raises_duplicate_mesh_dim_names(self):
        with self.assertRaisesRegex(
            RuntimeError,
            "Each mesh_dim_name must be unique.",
        ):
            mesh = init_device_mesh(
                self.device_type,
                (2, 4),
                mesh_dim_names=["dp", "dp"],
            )

    @with_comms
    def test_raises_mesh_shape_mesh_dim_names_mismatch(self):
        with self.assertRaisesRegex(
            RuntimeError,
            "mesh_shape and mesh_dim_names should have same length!",
        ):
            mesh = init_device_mesh(
                self.device_type,
                (8,),
                mesh_dim_names=["dp", "tp"],
            )


class TestDeviceMeshGetItem(DTensorTestBase):
    @property
    def world_size(self):
        return 8

    @with_comms
    def test_raises_mesh_dim_less_than_2(self):
        with self.assertRaisesRegex(RuntimeError, "Cannot slice a DeviceMesh"):
            mesh = init_device_mesh(self.device_type, (8,))
            child_mesh = mesh["DP"]

    @with_comms
    def test_raises_no_mesh_dim_found(self):
        with self.assertRaisesRegex(KeyError, "No `mesh_dim_names` found."):
            mesh = init_device_mesh(self.device_type, (2, 4))
            child_mesh = mesh["DP"]

    @with_comms
    def test_raises_invalid_mesh_dim_name(self):
        child_mesh_dim_name = "PP"
        with self.assertRaisesRegex(
            KeyError, f"Mesh dimension '{child_mesh_dim_name}' does not exist."
        ):
            mesh_dim_names = ("DP", "TP")
            mesh = init_device_mesh(
                self.device_type, (2, 4), mesh_dim_names=mesh_dim_names
            )
            child_mesh = mesh[child_mesh_dim_name]

    @with_comms
    def test_get_item(self):
        mesh_shape = (2, 4)
        mesh_dim_names = ("DP", "TP")
        mesh_2d = init_device_mesh(
            self.device_type, mesh_shape, mesh_dim_names=mesh_dim_names
        )

        pg_ranks_by_dim_name = {}
        for mesh_dim_name in mesh_dim_names:
            mesh_dim = mesh_dim_names.index(mesh_dim_name)
            pg_ranks_by_dim_name[mesh_dim_name] = mesh_2d.mesh.swapdims(
                -1, mesh_dim
            ).reshape(-1, mesh_2d.mesh.size(mesh_dim))

        tp_mesh = mesh_2d["TP"]
        tp_group_idx = self.rank // 4
        self.assertEqual(tp_mesh.mesh, pg_ranks_by_dim_name["TP"][tp_group_idx])

        dp_mesh = mesh_2d["DP"]
        dp_group_idx = self.rank % 4
        self.assertEqual(mesh_2d["DP"].mesh, pg_ranks_by_dim_name["DP"][dp_group_idx])

    @with_comms
    def test_get_item_1d(self):
        mesh = init_device_mesh(self.device_type, (8,), mesh_dim_names=("dp",))
        # Make sure slicing out 1D mesh from a 1D mesh works.
        # We are just dummy return without the parent mesh here.
        dp_mesh = mesh["dp"]
        self.assertEqual(dp_mesh, mesh)

        with self.assertRaisesRegex(RuntimeError, "Invalid mesh_dim_name"):
            dp_mesh = mesh["dim0"]


class TestMeshEnv(DTensorTestBase):
    @with_comms
    def test_get_parent_mesh(self):
        mesh_shape = (2, self.world_size // 2)
        mesh_dim_names = ("DP", "TP")
        mesh_2d = init_device_mesh(
            self.device_type, mesh_shape, mesh_dim_names=mesh_dim_names
        )

        self.assertEqual(_mesh_resources.get_parent_mesh(mesh_2d["DP"]), mesh_2d)
        self.assertEqual(_mesh_resources.get_parent_mesh(mesh_2d["TP"]), mesh_2d)

        mesh_0_2 = DeviceMesh(self.device_type, [0, 2])
        mesh_1_3 = DeviceMesh(self.device_type, [1, 3])

        self.assertEqual(_mesh_resources.get_parent_mesh(mesh_2d["DP"]), mesh_2d)
        self.assertEqual(_mesh_resources.get_parent_mesh(mesh_2d["TP"]), mesh_2d)
        self.assertEqual(_mesh_resources.get_parent_mesh(mesh_0_2), None)
        self.assertEqual(_mesh_resources.get_parent_mesh(mesh_1_3), None)

    @with_comms
    def test_get_parent_mesh_dim_exist(self):
        mesh_shape = (2, self.world_size // 2)
        mesh_dim_names = ("DP", "TP")
        mesh_2d = init_device_mesh(
            self.device_type, mesh_shape, mesh_dim_names=mesh_dim_names
        )

        self.assertEqual(_mesh_resources.get_parent_mesh_dim(mesh_2d["DP"]), 0)
        self.assertEqual(_mesh_resources.get_parent_mesh_dim(mesh_2d["TP"]), 1)

    @with_comms
    def test_get_parent_mesh_dim_not_exist(self):
        mesh_shape = (self.world_size,)
        mesh = init_device_mesh(self.device_type, mesh_shape)

        self.assertEqual(_mesh_resources.get_parent_mesh_dim(mesh), None)

    @with_comms
    def test_get_mesh_dim_by_name(self):
        mesh_shape = (2, self.world_size // 2)
        mesh_dim_names = ("DP", "TP")
        mesh_2d = init_device_mesh(
            self.device_type, mesh_shape, mesh_dim_names=mesh_dim_names
        )

        self.assertEqual(_mesh_resources.get_mesh_dim_by_name(mesh_2d, "DP"), 0)
        self.assertEqual(_mesh_resources.get_mesh_dim_by_name(mesh_2d, "TP"), 1)


class DeviceMeshCollectiveTest(DTensorTestBase):
    @property
    def world_size(self):
        return 8

    @with_comms
    def test_broadcast_1d(self):
        mesh = DeviceMesh(self.device_type, torch.arange(self.world_size))
        local_tensor = torch.ones(3, 3, device=self.device_type) * self.rank
        mesh_broadcast(local_tensor, mesh, mesh_dim=0)
        self.assertEqual(local_tensor, torch.zeros(3, 3))

    @with_comms
    def test_scatter_1d(self):
        mesh = DeviceMesh(self.device_type, torch.arange(self.world_size))
        scatter_tensor_shape = [3, 3, 3]
        for scatter_dim in range(len(scatter_tensor_shape)):
            shard_placement = Shard(scatter_dim)
            scatter_tensor_shape[scatter_dim] *= self.world_size
            # make the random seed same across rank
            torch.manual_seed(0)
            global_tensor = torch.randn(scatter_tensor_shape, device=self.device_type)
            splitted_list, _ = shard_placement._split_tensor(
                global_tensor, mesh.size(), with_padding=True, contiguous=True
            )
            recv_tensor = torch.empty_like(splitted_list[mesh.get_rank()])
            # scatter on dim > 0 would generate non-contiguous tensor, verify that works
            mesh_scatter(recv_tensor, splitted_list, mesh, mesh_dim=0)
            self.assertEqual(recv_tensor, splitted_list[mesh.get_rank()])

    @with_comms
    def test_scatter_uneven(self):
        device_mesh = DeviceMesh(self.device_type, list(range(self.world_size)))
        my_rank = device_mesh.get_rank()
        tensor_to_split = torch.randn(
            device_mesh.size() + 3, device_mesh.size() + 1, device=self.device_type
        )

        for shard_dim in range(tensor_to_split.ndim):
            shard_placement = Shard(shard_dim)

            tensor_to_scatter = tensor_to_split.clone()
            tensor_splitted_list = list(
                torch.chunk(tensor_to_split, self.world_size, dim=shard_dim)
            )
            for _ in range(self.world_size - len(tensor_splitted_list)):
                tensor_splitted_list.append(torch.tensor([], device=self.device_type))

            padded_tensor_list, pad_sizes = shard_placement._split_tensor(
                tensor_to_scatter,
                device_mesh.size(),
                with_padding=True,
                contiguous=True,
            )

            scattered_tensor = torch.empty_like(padded_tensor_list[my_rank])
            mesh_scatter(scattered_tensor, padded_tensor_list, device_mesh, mesh_dim=0)

            if pad_sizes[my_rank] != 0:
                scattered_tensor = shard_placement._unpad_tensor(
                    scattered_tensor, pad_sizes[my_rank]
                )

            if scattered_tensor.numel() == 0:
                # We need to check numel() instead of size if a tensor is ([]) after unpadding,
                # since the size could be ([0, 8]) after unpadding.
                self.assertEqual(
                    scattered_tensor.numel(), tensor_splitted_list[my_rank].numel()
                )
            else:
                self.assertEqual(
                    scattered_tensor.size(), tensor_splitted_list[my_rank].size()
                )
                self.assertEqual(scattered_tensor, tensor_splitted_list[my_rank])

    @with_comms
    def test_all_gather_uneven(self):
        device_mesh = DeviceMesh(self.device_type, list(range(self.world_size)))
        my_rank = device_mesh.get_rank()
        tensor_to_split = torch.ones(
            device_mesh.size() + 3,
            device_mesh.size() + 1,
            device=self.device_type,
        )

        for shard_dim in range(tensor_to_split.ndim):
            shard_placement = Shard(shard_dim)
            tensor_padded_list, pad_sizes = shard_placement._split_tensor(
                tensor_to_split,
                device_mesh.size(),
                with_padding=True,
                contiguous=True,
            )
            local_tensor = tensor_padded_list[my_rank]
            big_tensor = funcol.all_gather_tensor(
                local_tensor, gather_dim=shard_dim, group=(device_mesh, 0)
            )
            big_tensor_chunks = list(
                torch.chunk(big_tensor, device_mesh.size(), dim=shard_dim)
            )
            unpadded_list = [
                shard_placement._unpad_tensor(big_tensor_chunks[i], pad_sizes[i])
                if pad_sizes[i] > 0
                else big_tensor_chunks[i]
                for i, big_tensor in enumerate(big_tensor_chunks)
            ]
            all_gathered_tensor = torch.cat(unpadded_list, dim=shard_dim)

            self.assertEqual(all_gathered_tensor.size(), tensor_to_split.size())
            self.assertEqual(all_gathered_tensor, tensor_to_split)

    @with_comms
    def test_reduce_scatter_contiguous(self):
        device_mesh = DeviceMesh(self.device_type, list(range(self.world_size)))
        my_rank = device_mesh.get_rank()

        # Init the tensor
        step = self.world_size * 2
        total_elem = step**2
        tensor = torch.arange(0, total_elem).view(step, -1).to(device=self.device_type)
        tensor = tensor * (my_rank + 1)

        # Get non-contiguous tensor by slicing
        tensor_to_reduce = tensor[::2, :2]
        tensor_contiguous = tensor_to_reduce.clone().contiguous()

        # Partial to Shard to trigger reduce_scatter
        tensor_to_reduce = DTensor.from_local(
            tensor_to_reduce, device_mesh, [_Partial()]
        )
        tensor_contiguous = DTensor.from_local(
            tensor_contiguous, device_mesh, [_Partial()]
        )
        new_tensor = tensor_to_reduce.redistribute(device_mesh, [Shard(0)])
        new_tensor_contiguous = tensor_contiguous.redistribute(device_mesh, [Shard(0)])

        # The output for contiguous and non-contiguous tensors of the same value
        # should return the same reducescatter value.
        new_tensor_local = new_tensor._local_tensor
        new_tensor_contiguous_local = new_tensor_contiguous._local_tensor
        self.assertEqual(new_tensor_local, new_tensor_contiguous_local)
        self.assertEqual(list(new_tensor_local.size()), [1, 2])

        # Check the reduce numerical value
        sum_base = (1 + self.world_size) * self.world_size / 2
        first_elem = my_rank * sum_base * step * 2
        expected_tensor = torch.tensor(
            [[first_elem, first_elem + sum_base]],
            dtype=new_tensor_local.dtype,
            device=self.device_type,
        )
        self.assertEqual(new_tensor_local, expected_tensor)

    @with_comms
    def test_reduce_scatter_uneven(self):
        device_mesh = DeviceMesh(self.device_type, list(range(self.world_size)))
        my_rank = device_mesh.get_rank()
        tensor_to_split = (
            torch.ones(
                device_mesh.size() + 3,
                device_mesh.size() + 1,
                device=self.device_type,
            )
            * self.rank
        )

        for shard_dim in range(tensor_to_split.ndim):
            shard_placement = Shard(shard_dim)
            tensor_to_scatter = tensor_to_split.clone()

            tensor_splitted_list = list(
                torch.chunk(tensor_to_split, self.world_size, dim=shard_dim)
            )
            for _ in range(self.world_size - len(tensor_splitted_list)):
                tensor_splitted_list.append(torch.tensor([], device=self.device_type))

            padded_tensor_list, pad_sizes = shard_placement._split_tensor(
                tensor_to_scatter,
                device_mesh.size(),
                with_padding=True,
                contiguous=True,
            )

            tensor_to_reduce = torch.cat(padded_tensor_list, shard_dim)

            res_num = ((0 + self.world_size - 1) * self.world_size) / 2

            scattered_tensor = funcol.reduce_scatter_tensor(
                tensor_to_reduce,
                reduceOp="sum",
                scatter_dim=shard_dim,
                group=(device_mesh, 0),
            )

            # unpad scattered_tensor
            if pad_sizes[my_rank] > 0:
                scattered_tensor = shard_placement._unpad_tensor(
                    scattered_tensor, pad_sizes[my_rank]
                )

            if scattered_tensor.numel() == 0:
                # We need to check numel() instead of size if a tensor is ([]) after unpadding,
                # since the size could be ([0, 8]) after unpadding.
                self.assertEqual(
                    scattered_tensor.numel(), tensor_splitted_list[my_rank].numel()
                )
            else:
                self.assertEqual(
                    scattered_tensor.size(), tensor_splitted_list[my_rank].size()
                )
                self.assertEqual(
                    scattered_tensor,
                    torch.ones_like(tensor_splitted_list[my_rank]) * res_num,
                )

    @with_comms
    def test_broadcast_nd(self):
        mesh_tensor = torch.arange(8).reshape(2, 2, 2)
        mesh = DeviceMesh(self.device_type, mesh_tensor)
        local_tensor = torch.ones(3, 3, device=self.device_type) * self.rank

        # check all dim groups
        dim_to_subgroups = mesh.get_group()
        for dim, dim_group in enumerate(dim_to_subgroups):
            dim_group_size = get_world_size(dim_group)
            global_ranks = [
                get_global_rank(dim_group, i) for i in range(dim_group_size)
            ]
            cloned_local_tensor = local_tensor.clone()
            mesh_broadcast(cloned_local_tensor, mesh, mesh_dim=dim)
            res_num = global_ranks[0]
            self.assertEqual(cloned_local_tensor, torch.ones(3, 3) * res_num)

    @with_comms
    def test_scatter_nd(self):
        mesh_tensor = torch.arange(8).reshape(2, 2, 2)
        mesh = DeviceMesh(self.device_type, mesh_tensor)

        # check all dim groups
        dim_to_subgroups = mesh.get_group()
        for dim, dim_group in enumerate(dim_to_subgroups):
            dim_group_size = get_world_size(dim_group)
            global_ranks = [
                get_global_rank(dim_group, i) for i in range(dim_group_size)
            ]
            scattered_tensors = [
                torch.ones(3, 3, device=self.device_type) * global_rank
                for global_rank in global_ranks
            ]
            received_tensor = torch.empty_like(
                scattered_tensors[mesh.get_coordinate()[dim]]
            )
            mesh_scatter(received_tensor, scattered_tensors, mesh, mesh_dim=dim)
            self.assertEqual(received_tensor, torch.ones(3, 3) * self.rank)

    @with_comms
    def test_all_to_all_1d(self):
        # transpose on a 2D tensor distributed over N nodes:
        mesh = DeviceMesh(self.device_type, torch.arange(self.world_size))
        tensor_shape = [3, 3]
        input_tensor_list = [
            torch.ones(*tensor_shape, device=self.device_type)
            * (rank + self.rank * self.world_size)
            for rank in range(self.world_size)
        ]
        expected_tensor_list = [
            torch.ones(tensor_shape, device=self.device_type)
            * (self.rank + rank * self.world_size)  # i.e. transpose
            for rank in range(self.world_size)
        ]
        for scatter_dim in range(len(tensor_shape)):
            output_tensor_list = [
                torch.empty_like(input_tensor_list[idx])
                for idx in range(len(input_tensor_list))
            ]
            # scatter on dim > 0 would generate non-contiguous tensor, verify that works
            mesh_all_to_all(output_tensor_list, input_tensor_list, mesh, mesh_dim=0)
            output_tensor = torch.cat(output_tensor_list, dim=scatter_dim)
            expected_tensor = torch.cat(expected_tensor_list, dim=scatter_dim)

            self.assertEqual(output_tensor, expected_tensor)

    @with_comms
    def test_all_to_all_nd(self):
        mesh_tensor = torch.arange(8).reshape(2, 2, 2)
        mesh = DeviceMesh(self.device_type, mesh_tensor)
        tensor_shape = [3, 3, 3]
        # check all dim groups
        dim_to_subgroups = mesh.get_group()
        for dim, dim_group in enumerate(dim_to_subgroups):
            my_coordinate = mesh.get_coordinate()[dim]
            dim_group_size = get_world_size(dim_group)
            global_ranks = [
                get_global_rank(dim_group, i) for i in range(dim_group_size)
            ]
            input_tensor_list = [
                torch.ones(*tensor_shape, device=self.device_type)
                * (i + self.rank * dim_group_size)
                for i in range(dim_group_size)
            ]
            expected_tensor_list = [
                torch.ones(*tensor_shape, device=self.device_type)
                * (my_coordinate + global_rank * dim_group_size)  # i.e. transpose
                for global_rank in global_ranks
            ]
            for scatter_dim in range(len(tensor_shape)):
                # input_tensor = torch.cat(input_tensor_list, dim=scatter_dim)
                output_tensor_list = [
                    torch.empty_like(input_tensor_list[idx])
                    for idx in range(len(input_tensor_list))
                ]
                # scatter on dim > 0 would generate non-contiguous tensor, verify that works
                mesh_all_to_all(
                    output_tensor_list, input_tensor_list, mesh, mesh_dim=dim
                )
                output_tensor = torch.cat(output_tensor_list, dim=scatter_dim)
                expected_tensor = torch.cat(expected_tensor_list, dim=scatter_dim)
                self.assertEqual(output_tensor, expected_tensor)


class DeviceMeshTestWithNativeFunCol(DeviceMeshTest):
    def setUp(self) -> None:
        self._prev_native_funcol_enabled = funcol.native_funcol_enabled()
        funcol.enable_native_funcol()
        super().setUp()

    def tearDown(self) -> None:
        super().tearDown()
        if not self._prev_native_funcol_enabled:
            funcol.disable_native_funcol()


class DeviceMeshCollectiveTestWithNativeFunCol(DeviceMeshCollectiveTest):
    def setUp(self) -> None:
        self._prev_native_funcol_enabled = funcol.native_funcol_enabled()
        funcol.enable_native_funcol()
        super().setUp()

    def tearDown(self) -> None:
        super().tearDown()
        if not self._prev_native_funcol_enabled:
            funcol.disable_native_funcol()


if __name__ == "__main__":
    run_tests()
