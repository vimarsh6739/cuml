/*
 * Copyright (c) 2020-2021, NVIDIA CORPORATION.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

#include <cuml/genetic/program.h>
#include <cuml/genetic/node.h>
#include <raft/cudart_utils.h>
#include <rmm/device_uvector.hpp>

#include <random>

#include "node.cuh"
#include "reg_stack.cuh"
#include "constants.h"
#include "fitness.cuh"

namespace cuml {
namespace genetic {

/** 
 * Execution kernel for a single program. We assume that the input data 
 * is stored in column major format.
 */
template<int MaxSize>
__global__ void execute_kernel(program_t p, float* data, float* y_pred, int n_rows) {

  // Single evaluation stack per thread
  stack<float, MaxSize> eval_stack;

  for(size_t row_idx = blockIdx.x*blockDim.x + threadIdx.x; row_idx < (size_t)n_rows;
      row_idx += blockDim.x * gridDim.x) {

    // Arithmetic expr stored in prefix form
    for(int e=p->len-1;e>=0;--e) {

      node* curr = &p->nodes[e];
      
      if(detail::is_terminal(curr->t)) {

        // function
        int ar = detail::arity(curr->t);
        float inval = ar > 0 ? eval_stack.pop() : 0.0f;
        ar--;
        float inval1 = ar > 0 ? eval_stack.pop() : 0.0f;
        ar--;
        eval_stack.push(detail::evaluate_node(*curr,data,n_rows,row_idx,inval,inval1));

      } else {

        // constant or variable
        eval_stack.push(detail::evaluate_node(*curr,data, n_rows, row_idx, 0.0f,0.0f ));
      }
    }

    y_pred[row_idx] = eval_stack.pop();

  }                                   
}

program::program(const program& src) : len(src.len), depth(src.depth), raw_fitness_(src.raw_fitness_){
  nodes = new node[len];
  for(int i=0;i<len;++i)
    nodes[i]=src.nodes[i];
}

/** 
 * Internal function which computes the score of program p on the given dataset.
 */
void metric(const raft::handle_t &h, program_t p, 
            int len, float* y, float* y_pred, 
            float* w, float* score) {
  // Call appropriate metric function based on metric defined in p
  cudaStream_t stream = h.get_stream();

  if(p->metric == metric_t::pearson){
    _weighted_pearson(stream, len, y, y_pred, w, score);
  } else if(p->metric == metric_t::spearman){
    _weighted_spearman(stream, len, y, y_pred, w, score);
  } else if(p->metric == metric_t::mae){
    _mean_absolute_error(stream, len, y, y_pred, w, score);
  } else if(p->metric == metric_t::mse){
    _mean_square_error(stream, len, y, y_pred, w, score);
  } else if(p->metric == metric_t::rmse){
    _root_mean_square_error(stream, len, y, y_pred, w, score);
  } else if(p->metric == metric_t::logloss){
    _log_loss(stream, len, y, y_pred, w, score);
  } else{
    // None of the above - error
  }
}
  
void execute_single(const raft::handle_t &h, program_t p, 
                     float* data, float* y_pred, int n_rows) {

  cudaStream_t stream = h.get_stream();

  execute_kernel<MAX_STACK_SIZE><<<raft::ceildiv(n_rows,GENE_TPB),
                                  GENE_TPB,0,stream>>>(p,
                                                      data, 
                                                      y_pred,
                                                      n_rows );
  CUDA_CHECK(cudaPeekAtLastError());
}

void raw_fitness(const raft::handle_t &h, program_t p, 
                 float* data, float* y, int num_rows, 
                 float* sample_weights, float* score){
    
  cudaStream_t stream = h.get_stream();

  rmm::device_uvector<float> y_pred(num_rows,stream);  
  
  execute_single(h, p, data, y_pred.data(), num_rows);
  metric(h, p, num_rows, y, y_pred.data(), sample_weights, score);
}   

void fitness(const raft::handle_t &h, program_t p, 
             float parsimony_coeff, float* score) { 
  float penalty = parsimony_coeff * p->len;
  *score = p->raw_fitness_ - penalty;
}

/**
 * Get a random subtree of the current program(on CPU)
 */
std::pair<int, int> get_subtree(program_t p, int seed) {
  
  int start,end;
  start=end=0;

  // Specify RNG
  std::mt19937 gen(seed);
  std::uniform_real_distribution<float> dist(0.0f, 1.0f);
  float bound = dist(gen);

  // Specify subtree start probs acc to Koza's selection approach 
  std::vector<float> node_probs(p->len,0.1);
  float sum = 0.1 * p->len;
  for(int i=0; i< p->len ; ++i){
    if(p->nodes[i].is_nonterminal()){
      node_probs[i] = 0.9; 
      sum += 0.8;
    }
  }

  // Normalize vector
  for(int i=0;i<p->len;++i){
    node_probs[i] /= sum;
  }

  start = std::lower_bound(node_probs.begin(),node_probs.end(),bound) - node_probs.begin();
  end = start;

  // Iterate until all function arguments are satisfied in current subtree
  int num_args = 1;
  while(num_args > end - start){
    node curr = p->nodes[end];
    if(curr.is_nonterminal())num_args += curr.arity();
    ++end;
  }

  return std::make_pair(start,end);
}

program_t point_mutation(program_t prog, param& params, int seed){
  
  program_t next_prog = new program(*prog);
  
  // Specify RNG
  std::mt19937 gen(seed);
  std::uniform_real_distribution<float> dist_01(0.0f, 1.0f);
  // Fill with uniform numbers
  std::vector<float> node_probs(next_prog->len);
  std::generate(node_probs.begin(),node_probs.end(),[&]{return dist_01(gen)});

  // Mutate nodes
  int len = next_prog->len;
  for(int i=0;i<len;++i){
    node curr = prog->nodes[i];
    if(node_probs[i] < params.p_point_replace){
      if(curr.is_terminal()){
        // Replace with a var or const
        std::uniform_int_distribution<> dist_t(0,params.num_features);
        int ch = dist_t(gen);
        if(ch == (params.num_features + 1)){
          // Add random constant
          std::uniform_real_distribution<float> dist_c(params.const_range.first,
                                                       params.const_range.second);
          next_prog->nodes[i] = *(new node(dist_c(gen)));
        }
        else{
          // Add variable ch
          next_prog->nodes[i] = *(new node(ch));
        }
      }
      else{
        // Replace current function with another function of the same arity
        std::uniform_int_distribution<> dist_nt(0,params.arity_set[curr.arity()].size()-1);
        next_prog->nodes[i] = *(new node(params.arity_set[curr.arity()][dist_nt(gen)]));
      }
    }
  }
}

} // namespace genetic
} // namespace cuml