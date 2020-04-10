# Copyright (c) 2019, NVIDIA CORPORATION.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

from cuml.dask.common import extract_ddf_partitions
from cuml.dask.common import workers_to_parts
from cuml.dask.common import parts_to_ranks
rfrom cuml.dask.common import aise_exception_from_futures
from cuml.dask.common import flatten_grouped_results 
from cuml.dask.common import raise_mg_import_exception
from cuml.dask.common.base import BaseEstimator

from dask.distributed import default_client
from cuml.dask.common.comms import worker_state, CommsContext
from dask.distributed import wait
from cuml.dask.common.input_utils import to_output
from cuml.dask.common.input_utils import DistributedDataHandler

from uuid import uuid1


def _func_get_d(f, idx):
    i, d = f
    return d[idx]


def _func_get_i(f, idx):
    i, d = f
    return i[idx]


class NearestNeighbors(BaseEstimator):
    """
    Multi-node Multi-GPU NearestNeighbors Model.
    """
    def __init__(self, client=None, streams_per_handle=0, verbose=False,
                 **kwargs):
        super(NearestNeighbors, self).__init__(client=client,
                                               verbose=verbose,
                                               **kwargs)

        self.streams_per_handle = streams_per_handle

    def fit(self, X):
        """
        Fit a multi-node multi-GPU Nearest Neighbors index

        Parameters
        ----------
        X : dask_cudf.Dataframe

        Returns
        -------
        self: NearestNeighbors model
        """
        self.X_handler = DistributedDataHandler.create(data=X, client=self.client)
        self.datatype = self.X_handler.datatype
        self.n_cols = X.shape[1]
        return self

    @staticmethod
    def _func_create_model(sessionId, **kwargs):
        try:
            from cuml.neighbors.nearest_neighbors_mg import \
                NearestNeighborsMG as cumlNN
        except ImportError:
            raise_mg_import_exception()

        handle = worker_state(sessionId)["handle"]
        return cumlNN(handle=handle, **kwargs)

    @staticmethod
    def _func_kneighbors(model, local_idx_parts, idx_m, n, idx_parts_to_ranks,
                         local_query_parts, query_m, query_parts_to_ranks,
                         rank, k):

        return model.kneighbors(
            local_idx_parts, idx_m, n, idx_parts_to_ranks,
            local_query_parts, query_m, query_parts_to_ranks,
            rank, k
        )

    @staticmethod
    def _build_comms(index_handler, query_handler, streams_per_handle,
                     verbose):
        # Communicator clique needs to include the union of workers hosting
        # query and index partitions
        workers = set(index_handler.workers)
        workers.update(query_handler.workers)

        comms = CommsContext(comms_p2p=True,
                             streams_per_handle=streams_per_handle,
                             verbose=verbose)
        comms.init(workers=workers)
        return comms

    def get_neighbors(self, n_neighbors):
        """
        Returns the default n_neighbors, initialized from the constructor,
        if n_neighbors is None.

        Parameters
        ----------
        n_neighbors : int
            Number of neighbors

        Returns
        --------
        n_neighbors: int
            Default n_neighbors if parameter n_neighbors is none
        """
        if n_neighbors is None:
            if "n_neighbors" in self.kwargs \
                    and self.kwargs["n_neighbors"] is not None:
                n_neighbors = self.kwargs["n_neighbors"]
            else:
                try:
                    from cuml.neighbors.nearest_neighbors_mg import \
                        NearestNeighborsMG as cumlNN
                except ImportError:
                    raise_mg_import_exception()
                n_neighbors = cumlNN().n_neighbors

        return n_neighbors

    def _create_models(self, comms):

        """
        Each Dask worker creates a single model
        """
        key = uuid1()
        nn_models = dict([(worker, self.client.submit(
            NearestNeighbors._func_create_model,
            comms.sessionId,
            **self.kwargs,
            workers=[worker],
            key="%s-%s" % (key, idx)))
            for idx, worker in enumerate(comms.worker_addresses)])

        return nn_models

    def _query_models(self, n_neighbors,
                      comms, nn_models,
                      index_handler, query_handler):

        worker_info = comms.worker_info(comms.worker_addresses)

        """
        Build inputs and outputs
        """
        index_handler.calculate_parts_to_sizes(comms=comms)
        query_handler.calculate_parts_to_sizes(comms=comms)

        idx_parts_to_ranks, idx_M = parts_to_ranks(self.client,
                                            worker_info,
                                            index_handler.gpu_futures)

        query_parts_to_ranks, query_M = parts_to_ranks(self.client,
                                                       worker_info,
                                                       query_handler.gpu_futures)

        """
        Invoke kneighbors on Dask workers to perform distributed query
        """

        key = uuid1()
        nn_fit = dict([(worker_info[worker]["rank"], self.client.submit(
                        NearestNeighbors._func_kneighbors,
                        nn_models[worker],
                        index_handler.worker_to_parts[worker] if
                        worker in index_handler.workers else [],
                        index_handler.total_rows,
                        self.n_cols,
                        idx_parts_to_ranks,
                        query_handler.worker_to_parts[worker] if
                        worker in query_handler.workers else [],
                        query_handler.total_rows,
                        query_parts_to_ranks,
                        worker_info[worker]["rank"],
                        n_neighbors,
                        key="%s-%s" % (key, idx),
                        workers=[worker]))
                       for idx, worker in enumerate(comms.worker_addresses)])

        wait(list(nn_fit.values()))
        raise_exception_from_futures(list(nn_fit.values()))

        """
        Gather resulting partitions and return dask_cudfs
        """
        out_d_futures = flatten_grouped_results(self.client,
                                                query_parts_to_ranks,
                                                nn_fit,
                                                getter_func=_func_get_d)

        out_i_futures = flatten_grouped_results(self.client,
                                                query_parts_to_ranks,
                                                nn_fit,
                                                getter_func=_func_get_i)

        return nn_fit, out_d_futures, out_i_futures

    def kneighbors(self, X=None, n_neighbors=None, return_distance=True,
                   _return_futures=False):
        """
        Query the distributed nearest neighbors index

        Parameters
        ----------
        X : dask_cudf.Dataframe
            Vectors to query. If not provided, neighbors of each indexed point
            are returned.
        n_neighbors : int
            Number of neighbors to query for each row in X. If not provided,
            the n_neighbors on the model are used.
        return_distance : boolean (default=True)
            If false, only indices are returned

        Returns
        -------
        ret : tuple (dask_cudf.DataFrame, dask_cudf.DataFrame)
            First dask-cuDF DataFrame contains distances, second conains the
            indices.
        """
        n_neighbors = self.get_neighbors(n_neighbors)

        query_handler = self.X_handler if X is None else \
            DistributedDataHandler.create(data=X, client=self.client)

        if query_handler is None:
            raise ValueError("Model needs to be trained using fit() "
                             "before calling kneighbors()")

        """
        Create communicator clique
        """
        comms = NearestNeighbors._build_comms(self.X_handler, query_handler,
                                              self.streams_per_handle,
                                              self.verbose)

        """
        Initialize models on workers
        """
        nn_models = self._create_models(comms)

        """
        Perform model query
        """
        nn_fit, out_d_futures, out_i_futures = \
            self._query_models(n_neighbors, comms, nn_models,
                               self.X_handler, query_handler)

        comms.destroy()

        if _return_futures:
            ret = nn_fit, out_i_futures if not return_distance else \
                (nn_fit, out_d_futures, out_i_futures)
        else:
            ret = to_output(out_i_futures, self.datatype) \
                if not return_distance else (to_output(out_d_futures,
                                             self.datatype), to_output(
                                                 out_i_futures,
                                                 self.datatype))

        return ret
