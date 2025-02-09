import argparse
import csv
import json
import os
from pathlib import Path
from typing import List, Union
import numpy as np
import torch
# for debug
# from qwen_7b_chat.tokenization_qwen import QWenTokenizer as AutoTokenizer
# for realease
from transformers import AutoTokenizer
import tensorrt_llm
from tensorrt_llm.runtime import (
    ModelConfig, SamplingConfig, GenerationSession, GenerationSequence,
)
from tensorrt_llm.runtime.generation import _tile_beam_width, Mapping
from build import get_engine_name  # isort:skip
from utils.utils import make_context
from default_config import default_config

now_dir = os.path.dirname(os.path.abspath(__file__))


# copy from tensorrt_llm/runtime/generation.py to debug
class QWenForCausalLMGenerationSession(GenerationSession):
    def __init__(
            self,
            model_config: ModelConfig,
            engine_buffer,
            mapping: Mapping,
            global_max_input_length=default_config.max_input_len,
            global_max_output_length=default_config.max_input_len + default_config.max_new_tokens,
            debug_mode=False,
        ):
        super().__init__(model_config, engine_buffer, mapping, debug_mode)
        self.global_max_input_length = global_max_input_length
        self.global_max_output_length = global_max_output_length

    def __setup_decoder(self, input_ids: torch.Tensor,
                        sampling_config: SamplingConfig,
                        input_lengths: torch.Tensor):
        '''Allocate buffers and setup the post-processing decoder kernel
        '''
        batch_size = input_lengths.shape[0]
        scfg = sampling_config  # just to make a shorter name, no other meaning
        self.top_k = torch.full([batch_size], scfg.top_k, dtype=torch.int32)
        self.top_p = torch.full([batch_size], scfg.top_p, dtype=torch.float32)
        self.temperature = torch.full([batch_size],
                                      scfg.temperature,
                                      dtype=torch.float32)
        self.repetition_penalty = torch.full([batch_size],
                                             scfg.repetition_penalty,
                                             dtype=torch.float32)
        if scfg.repetition_penalty == 1.0:
            self.repetition_penalty = None

        self.length_penalty = torch.FloatTensor([scfg.length_penalty])

        self.presence_penalty = torch.full([batch_size],
                                           scfg.presence_penalty,
                                           dtype=torch.float32)
        if scfg.presence_penalty == 0.0:
            self.presence_penalty = None
        assert (
            scfg.presence_penalty == 0.0 or scfg.repetition_penalty == 0.0
        ), f"presence_penalty({scfg.presence_penalty}) and repetition_penalty({scfg.repetition_penalty}) cannot be larger than 0.0 at the same time."
        self.min_length = torch.full([batch_size],
                                     scfg.min_length,
                                     dtype=torch.int32)

        if scfg.beam_search_diversity_rate is not None:
            self.beam_search_diversity_rate = torch.full(
                [batch_size],
                scfg.beam_search_diversity_rate,
                dtype=torch.float32)
        else:
            self.beam_search_diversity_rate = None

        if scfg.random_seed is not None:
            self.random_seed = torch.full([batch_size],
                                          scfg.random_seed,
                                          dtype=torch.int64)
        else:
            self.random_seed = None

        self.dynamic_decoder.setup(
            batch_size, scfg.num_beams, self.top_k, self.top_p,
            self.temperature, self.repetition_penalty, self.presence_penalty,
            self.min_length, self.length_penalty,
            self.beam_search_diversity_rate, self.random_seed, self.top_p_decay,
            self.top_p_min, self.top_p_reset_ids)

        assert scfg.end_id is not None, "end_id cannot be none"
        assert scfg.pad_id is not None, 'pad_id cannot be none'
        self.end_ids = torch.full((batch_size * scfg.num_beams, ),
                                  scfg.end_id,
                                  dtype=torch.int32,
                                  device=self.device)
        max_input_length = input_lengths.max()

        if input_ids.shape[0] != input_lengths.shape[0]:
            # dim 0 of input_ids is not batch size, which means remove_padding is enabled
            split_ids_list = list(
                torch.split(input_ids,
                            input_lengths.cpu().numpy().tolist(),
                            dim=1))
            padded_input_ids = torch.nested.to_padded_tensor(
                torch.nested.nested_tensor(split_ids_list, dtype=torch.int32),
                scfg.pad_id).cuda().reshape(batch_size, max_input_length)
        else:
            padded_input_ids = input_ids
        if scfg.num_beams > 1:
            tiled_input_ids = _tile_beam_width(padded_input_ids, scfg.num_beams)
            tiled_input_ids = tiled_input_ids.reshape(batch_size,
                                                      scfg.num_beams,
                                                      max_input_length)
            transposed_input_ids = tiled_input_ids.permute(2, 0, 1)
            self.output_ids = torch.cat(
                (transposed_input_ids,
                 torch.zeros(self.max_seq_length - max_input_length,
                             batch_size,
                             scfg.num_beams,
                             dtype=padded_input_ids.dtype,
                             device=padded_input_ids.device)))
        else:
            transposed_input_ids = padded_input_ids.permute(1, 0)
            self.output_ids = torch.cat(
                (transposed_input_ids,
                 torch.zeros(self.max_seq_length - max_input_length,
                             batch_size,
                             dtype=padded_input_ids.dtype,
                             device=padded_input_ids.device)))

        self.parent_ids = torch.zeros(
            (self.max_seq_length, batch_size, scfg.num_beams),
            dtype=torch.int32,
            device=self.device)

        if scfg.num_beams > 1 or scfg.output_cum_log_probs:
            self.cum_log_probs = torch.full((batch_size, scfg.num_beams),
                                            -1e20,
                                            dtype=torch.float32,
                                            device=self.device)
            self.cum_log_probs[:, 0] = 0.0
        else:
            self.cum_log_probs = None

        if scfg.output_log_probs:
            self.log_probs = torch.zeros(
                (self.max_new_tokens, batch_size, scfg.num_beams),
                dtype=torch.float32,
                device=self.device)
        else:
            self.log_probs = None

        self.finished = torch.zeros((batch_size, scfg.num_beams),
                                    dtype=torch.bool,
                                    device=self.device)

    def decode(
        self,
        input_ids: torch.Tensor,
        input_lengths: torch.Tensor,
        sampling_config: SamplingConfig,
        prompt_embedding_table: torch.Tensor = None,
        tasks: torch.Tensor = None,
        prompt_vocab_size: torch.Tensor = None,
    ):
        batch_size = input_lengths.size(0)
        max_input_length = torch.max(input_lengths).item()
        assert batch_size == self.batch_size, \
            "Given batch size is different from the one used in setup()," \
            "rerun the setup function with the new batch size to avoid buffer overflow."
        assert max_input_length == self.max_input_length, \
            "Given input length is large then the one used in setup()," \
            "rerun the setup function with the new max_input_length to avoid buffer overflow."
        ite = 0  # index of local batches, will always be 0 if pp_size = 1
        scfg = sampling_config

        self.__setup_decoder(input_ids, scfg, input_lengths)
        if not self.buffer_allocated:
            raise RuntimeError('Buffer not allocated, please call setup first!')

        sequence_limit_lengths = torch.full((batch_size, 1),
                                            self.max_seq_length,
                                            dtype=torch.int32,
                                            device=self.device)
        sequence_lengths = torch.full((batch_size * scfg.num_beams, 1),
                                      max_input_length,
                                      dtype=torch.int32,
                                      device=self.device)
        len_list = torch.arange(0,
                                self.max_seq_length,
                                dtype=torch.int32,
                                device=self.device).unsqueeze(0).expand(
                                    batch_size, -1)
        mask = (len_list >= input_lengths.unsqueeze(1)) & (len_list <
                                                           max_input_length)
        masked_tokens = torch.zeros((batch_size, self.max_seq_length),
                                    dtype=torch.int32,
                                    device=self.device).masked_fill_(mask, 1)

        cache_indirections = [
            torch.full((
                batch_size,
                scfg.num_beams,
                self.max_seq_length,
            ),
                       0,
                       dtype=torch.int32,
                       device=self.device),
            torch.full((
                batch_size,
                scfg.num_beams,
                self.max_seq_length,
            ),
                       0,
                       dtype=torch.int32,
                       device=self.device)
        ]  # ping-pong buffers

        if self.paged_kv_cache:
            # Add sequences to the manager
            for bi in range(batch_size):
                generation_sequence = GenerationSequence(seq_idx=bi,
                                                         batch_idx=bi)
                self.kv_cache_manager.add_sequence(generation_sequence,
                                                   input_ids.size(1))

        kv_cache_block_pointers = []
        # start context phase
        for step in range(0, self.max_new_tokens):
            if self.paged_kv_cache:
                kv_cache_block_pointers = self.kv_cache_manager.get_pointer_arrays(
                )

            if step % 2:
                context = self.runtime.context_0
                this_src_cache_indirection = cache_indirections[1]
                this_tgt_cache_indirection = cache_indirections[0]
                next_src_cache_indirection = cache_indirections[0]
            else:
                context = self.runtime.context_1
                this_src_cache_indirection = cache_indirections[0]
                this_tgt_cache_indirection = cache_indirections[1]
                next_src_cache_indirection = cache_indirections[1]

            if step == 0:
                model_inputs = self._prepare_context_inputs(
                    batch_size=batch_size,
                    input_lengths=input_lengths,
                    use_gpt_attention_plugin=self.use_gpt_attention_plugin,
                    remove_input_padding=self.remove_input_padding,
                    max_input_length=max_input_length,
                    input_ids=input_ids,
                    pad_id=scfg.pad_id)

                position_ids = model_inputs.get('position_ids')
                last_token_ids = model_inputs.get('last_token_ids')
                attention_mask = model_inputs.get('attention_mask', None)

                ctx_shape, ctx_buffer = self._get_context_shape_buffer(
                    input_ids, max_input_length, step, masked_tokens,
                    input_lengths, position_ids, last_token_ids, attention_mask,
                    this_src_cache_indirection, kv_cache_block_pointers,
                    prompt_embedding_table, tasks, prompt_vocab_size)
                self.runtime._set_shape(context, ctx_shape)
                self.runtime._set_buffer(context, ctx_buffer)
                if self.debug_mode:
                    debug_buffer = ctx_buffer

            # dynamic_decoder currently use torch's current stream, so must let TRT enqueue use same stream here
            stream = torch.cuda.current_stream().cuda_stream
            ok = self.runtime._run(context, stream)
            if not ok:
                raise RuntimeError('Executing TRT engine failed!')
            if self.debug_mode:
                torch.cuda.synchronize()
                if step == 0:
                    print(debug_buffer.keys())
                    for key in debug_buffer.keys():
                        if key.startswith("layers."):
                            print(
                                f"{key} shape, mean, sum",
                                debug_buffer[key].shape,
                                debug_buffer[key].mean(),
                                debug_buffer[key].sum(),
                            )

            if step == 0 and scfg.num_beams > 1:

                if not self.use_gpt_attention_plugin:
                    attention_mask = _tile_beam_width(attention_mask,
                                                      scfg.num_beams)
                input_lengths = _tile_beam_width(input_lengths, scfg.num_beams)
                if self.use_gpt_attention_plugin:
                    self.sequence_length_buffer = _tile_beam_width(
                        self.sequence_length_buffer, scfg.num_beams)
                masked_tokens = _tile_beam_width(masked_tokens, scfg.num_beams)

                # Move tiling before logit computing of context
                for key in self.buffer.keys():
                    if "present_key_value" in key:
                        self.buffer[key] = _tile_beam_width(
                            self.buffer[key], scfg.num_beams)
                self.buffer['logits'] = _tile_beam_width(
                    self.buffer['logits'], scfg.num_beams)

            if not step == self.max_new_tokens - 1:
                # Set shape and address for the next step
                model_inputs = self._prepare_generation_inputs(
                    batch_size=batch_size,
                    input_lengths=input_lengths,
                    use_gpt_attention_plugin=self.use_gpt_attention_plugin,
                    remove_input_padding=self.remove_input_padding,
                    step=step,
                    num_beams=scfg.num_beams,
                    attention_mask=attention_mask,
                )

                position_ids = model_inputs.get('position_ids')
                last_token_ids = model_inputs.get('last_token_ids')
                attention_mask = model_inputs.get('attention_mask', None)

                next_context = self.runtime.context_1 if step % 2 else self.runtime.context_0
                next_step_shape, next_step_buffer = self._get_next_step_shape_buffer(
                    batch_size, scfg.num_beams, max_input_length, step,
                    masked_tokens, input_lengths, position_ids, last_token_ids,
                    attention_mask, next_src_cache_indirection,
                    kv_cache_block_pointers, prompt_embedding_table, tasks,
                    prompt_vocab_size)
                self.runtime._set_shape(next_context, next_step_shape)
                self.runtime._set_buffer(next_context, next_step_buffer)
                if self.debug_mode:
                    self.debug_buffer = next_step_buffer

            logits = self.buffer['logits']
            if logits is not None:
                # [batch_size x scft.num_beams, vocab_size_padded] -> [batch_size, scfg.num_beams, vocab_size_padded]
                next_token_logits = logits.reshape(
                    (batch_size, scfg.num_beams, -1)).to(torch.float32)
                decode_step = step + max_input_length
                should_stop = self.dynamic_decoder.forward(
                    next_token_logits, decode_step, max_input_length, ite,
                    batch_size, self.end_ids, self.top_k, self.top_p,
                    self.temperature, self.repetition_penalty,
                    self.presence_penalty, self.min_length, self.length_penalty,
                    self.beam_search_diversity_rate, self.top_p_decay,
                    self.top_p_min, self.top_p_reset_ids,
                    self.embedding_bias_opt, input_lengths,
                    sequence_limit_lengths, self.stop_words_list,
                    self.bad_words_list, this_src_cache_indirection,
                    self.output_ids, self.finished, sequence_lengths,
                    self.cum_log_probs, self.log_probs, self.parent_ids,
                    this_tgt_cache_indirection)

                if should_stop.item():
                    if self.paged_kv_cache:
                        # Free all blocks in all sequences.
                        # With in-flight batching and while loop we'll free some sequences, when they are done
                        self.kv_cache_manager.step([True] * batch_size *
                                                   scfg.num_beams)

                    # output shape of self.gather_tree: [batch_size, beam_width, output_len]
                    final_output_ids = self.gather_tree(
                        sequence_lengths, self.output_ids, self.parent_ids,
                        self.end_ids, input_lengths, batch_size, scfg.num_beams,
                        max_input_length, self.max_seq_length)
                    return final_output_ids, step + 1

            if self.paged_kv_cache and step < self.max_new_tokens - 1:
                # Iterate to the next step in KV cache manager.
                # Increase number of tokens for all unfinished sequences.
                # And allocate new blocks if needed.
                # We set this to False for all sequences, since we use only length criterion to stop now
                self.kv_cache_manager.step([False] * batch_size *
                                           scfg.num_beams)

        if self.paged_kv_cache:
            # Free all blocks in all sequences.
            # With in-flight batching and while loop we'll free some sequences, when they are done
            self.kv_cache_manager.step([True] * batch_size * scfg.num_beams)

        # output shape of self.gather_tree: [batch_size, beam_width, output_len]
        final_output_ids = self.gather_tree(sequence_lengths, self.output_ids,
                                            self.parent_ids, self.end_ids,
                                            input_lengths, batch_size,
                                            scfg.num_beams, max_input_length,
                                            self.max_seq_length)

        return final_output_ids, self.max_new_tokens
    
    def steam_decode(
        self,
        input_ids: torch.Tensor,
        input_lengths: torch.Tensor,
        sampling_config: SamplingConfig,
        prompt_embedding_table: torch.Tensor = None,
        tasks: torch.Tensor = None,
        prompt_vocab_size: torch.Tensor = None,
    ):
        batch_size = input_lengths.size(0)
        max_input_length = torch.max(input_lengths).item()
        assert batch_size == self.batch_size, \
            "Given batch size is different from the one used in setup()," \
            "rerun the setup function with the new batch size to avoid buffer overflow."
        assert max_input_length == self.max_input_length, \
            "Given input length is large then the one used in setup()," \
            "rerun the setup function with the new max_input_length to avoid buffer overflow."
        ite = 0  # index of local batches, will always be 0 if pp_size = 1
        scfg = sampling_config

        self.__setup_decoder(input_ids, scfg, input_lengths)
        if not self.buffer_allocated:
            raise RuntimeError('Buffer not allocated, please call setup first!')

        sequence_limit_lengths = torch.full((batch_size, 1),
                                            self.max_seq_length,
                                            dtype=torch.int32,
                                            device=self.device)
        sequence_lengths = torch.full((batch_size * scfg.num_beams, 1),
                                      max_input_length,
                                      dtype=torch.int32,
                                      device=self.device)
        len_list = torch.arange(0,
                                self.max_seq_length,
                                dtype=torch.int32,
                                device=self.device).unsqueeze(0).expand(
                                    batch_size, -1)
        mask = (len_list >= input_lengths.unsqueeze(1)) & (len_list <
                                                           max_input_length)
        masked_tokens = torch.zeros((batch_size, self.max_seq_length),
                                    dtype=torch.int32,
                                    device=self.device).masked_fill_(mask, 1)

        cache_indirections = [
            torch.full((
                batch_size,
                scfg.num_beams,
                self.max_seq_length,
            ),
                       0,
                       dtype=torch.int32,
                       device=self.device),
            torch.full((
                batch_size,
                scfg.num_beams,
                self.max_seq_length,
            ),
                       0,
                       dtype=torch.int32,
                       device=self.device)
        ]  # ping-pong buffers

        if self.paged_kv_cache:
            # Add sequences to the manager
            for bi in range(batch_size):
                generation_sequence = GenerationSequence(seq_idx=bi,
                                                         batch_idx=bi)
                self.kv_cache_manager.add_sequence(generation_sequence,
                                                   input_ids.size(1))

        kv_cache_block_pointers = []
        # start context phase
        for step in range(0, self.max_new_tokens):
            if self.paged_kv_cache:
                kv_cache_block_pointers = self.kv_cache_manager.get_pointer_arrays(
                )

            if step % 2:
                context = self.runtime.context_0
                this_src_cache_indirection = cache_indirections[1]
                this_tgt_cache_indirection = cache_indirections[0]
                next_src_cache_indirection = cache_indirections[0]
            else:
                context = self.runtime.context_1
                this_src_cache_indirection = cache_indirections[0]
                this_tgt_cache_indirection = cache_indirections[1]
                next_src_cache_indirection = cache_indirections[1]

            if step == 0:
                model_inputs = self._prepare_context_inputs(
                    batch_size=batch_size,
                    input_lengths=input_lengths,
                    use_gpt_attention_plugin=self.use_gpt_attention_plugin,
                    remove_input_padding=self.remove_input_padding,
                    max_input_length=max_input_length,
                    input_ids=input_ids,
                    pad_id=scfg.pad_id)

                position_ids = model_inputs.get('position_ids')
                last_token_ids = model_inputs.get('last_token_ids')
                attention_mask = model_inputs.get('attention_mask', None)

                ctx_shape, ctx_buffer = self._get_context_shape_buffer(
                    input_ids, max_input_length, step, masked_tokens,
                    input_lengths, position_ids, last_token_ids, attention_mask,
                    this_src_cache_indirection, kv_cache_block_pointers,
                    prompt_embedding_table, tasks, prompt_vocab_size)
                self.runtime._set_shape(context, ctx_shape)
                self.runtime._set_buffer(context, ctx_buffer)
                if self.debug_mode:
                    debug_buffer = ctx_buffer

            # dynamic_decoder currently use torch's current stream, so must let TRT enqueue use same stream here
            stream = torch.cuda.current_stream().cuda_stream
            ok = self.runtime._run(context, stream)
            if not ok:
                raise RuntimeError('Executing TRT engine failed!')
            if self.debug_mode:
                torch.cuda.synchronize()
                if step == 0:
                    print(debug_buffer.keys())

            if step == 0 and scfg.num_beams > 1:

                if not self.use_gpt_attention_plugin:
                    attention_mask = _tile_beam_width(attention_mask,
                                                      scfg.num_beams)
                input_lengths = _tile_beam_width(input_lengths, scfg.num_beams)
                if self.use_gpt_attention_plugin:
                    self.sequence_length_buffer = _tile_beam_width(
                        self.sequence_length_buffer, scfg.num_beams)
                masked_tokens = _tile_beam_width(masked_tokens, scfg.num_beams)

                # Move tiling before logit computing of context
                for key in self.buffer.keys():
                    if "present_key_value" in key:
                        self.buffer[key] = _tile_beam_width(
                            self.buffer[key], scfg.num_beams)
                self.buffer['logits'] = _tile_beam_width(
                    self.buffer['logits'], scfg.num_beams)

            if not step == self.max_new_tokens - 1:
                # Set shape and address for the next step
                model_inputs = self._prepare_generation_inputs(
                    batch_size=batch_size,
                    input_lengths=input_lengths,
                    use_gpt_attention_plugin=self.use_gpt_attention_plugin,
                    remove_input_padding=self.remove_input_padding,
                    step=step,
                    num_beams=scfg.num_beams,
                    attention_mask=attention_mask,
                )

                position_ids = model_inputs.get('position_ids')
                last_token_ids = model_inputs.get('last_token_ids')
                attention_mask = model_inputs.get('attention_mask', None)

                next_context = self.runtime.context_1 if step % 2 else self.runtime.context_0
                next_step_shape, next_step_buffer = self._get_next_step_shape_buffer(
                    batch_size, scfg.num_beams, max_input_length, step,
                    masked_tokens, input_lengths, position_ids, last_token_ids,
                    attention_mask, next_src_cache_indirection,
                    kv_cache_block_pointers, prompt_embedding_table, tasks,
                    prompt_vocab_size)
                self.runtime._set_shape(next_context, next_step_shape)
                self.runtime._set_buffer(next_context, next_step_buffer)
                if self.debug_mode:
                    self.debug_buffer = next_step_buffer

            logits = self.buffer['logits']
            if logits is not None:
                # [batch_size x scft.num_beams, vocab_size_padded] -> [batch_size, scfg.num_beams, vocab_size_padded]
                next_token_logits = logits.reshape(
                    (batch_size, scfg.num_beams, -1)).to(torch.float32)
                decode_step = step + max_input_length
                should_stop = self.dynamic_decoder.forward(
                    next_token_logits, decode_step, max_input_length, ite,
                    batch_size, self.end_ids, self.top_k, self.top_p,
                    self.temperature, self.repetition_penalty,
                    self.presence_penalty, self.min_length, self.length_penalty,
                    self.beam_search_diversity_rate, self.top_p_decay,
                    self.top_p_min, self.top_p_reset_ids,
                    self.embedding_bias_opt, input_lengths,
                    sequence_limit_lengths, self.stop_words_list,
                    self.bad_words_list, this_src_cache_indirection,
                    self.output_ids, self.finished, sequence_lengths,
                    self.cum_log_probs, self.log_probs, self.parent_ids,
                    this_tgt_cache_indirection)

                if should_stop.item():
                    if self.paged_kv_cache:
                        # Free all blocks in all sequences.
                        # With in-flight batching and while loop we'll free some sequences, when they are done
                        self.kv_cache_manager.step([True] * batch_size *
                                                   scfg.num_beams)

                    # output shape of self.gather_tree: [batch_size, beam_width, output_len]
                    final_output_ids = self.gather_tree(
                        sequence_lengths, self.output_ids, self.parent_ids,
                        self.end_ids, input_lengths, batch_size, scfg.num_beams,
                        max_input_length, self.max_seq_length)
                    yield final_output_ids, step + 1
                    break

            if self.paged_kv_cache and step < self.max_new_tokens - 1:
                # Iterate to the next step in KV cache manager.
                # Increase number of tokens for all unfinished sequences.
                # And allocate new blocks if needed.
                # We set this to False for all sequences, since we use only length criterion to stop now
                self.kv_cache_manager.step([False] * batch_size *
                                           scfg.num_beams)
            final_output_ids = self.gather_tree(
                sequence_lengths, self.output_ids, self.parent_ids,
                self.end_ids, input_lengths, batch_size, scfg.num_beams,
                max_input_length, self.max_seq_length
            )
            yield final_output_ids, step + 1

        if self.paged_kv_cache:
            # Free all blocks in all sequences.
            # With in-flight batching and while loop we'll free some sequences, when they are done
            self.kv_cache_manager.step([True] * batch_size * scfg.num_beams)

        # # output shape of self.gather_tree: [batch_size, beam_width, output_len]
        # final_output_ids = self.gather_tree(sequence_lengths, self.output_ids,
        #                                     self.parent_ids, self.end_ids,
        #                                     input_lengths, batch_size,
        #                                     scfg.num_beams, max_input_length,
        #                                     self.max_seq_length)

        # return final_output_ids, self.max_new_tokens
    def prepare_for_chat(
        self,
        tokenizer,
        input_text: Union[str, List[str]],
        system_text: str = "You are a helpful assistant.",
        history: list = None,
        max_input_length: Union[int, None] = None,
    ):
        if max_input_length is None:
            max_input_length = self.global_max_input_length
        else:
            max_input_length = min(max_input_length, self.global_max_input_length)
        if history is None:
            history = []
        pad_id = tokenizer.im_end_id
        # prepare for batch inference
        if not isinstance(input_text, list):
            batch_text = [input_text]
        else:
            batch_text = input_text
        if len(history) > 0 and len(history[0]) and len(history[0][0]) > 0 \
                and not isinstance(history[0][0], list):
            history_list = [history]
        elif len(history) == 0:
            history_list = [[]]
        else:
            history_list = history
        input_ids = []
        input_lengths = []

        for line, history in zip(batch_text, history_list):
            # use make_content to generate prompt
            _, input_id_list = make_context(
                tokenizer=tokenizer,
                query=line,
                history=history,
                system=system_text,
                max_input_length=max_input_length,
            )
            # print("input_id_list len", len(input_id_list))
            input_id = torch.from_numpy(
                np.array(input_id_list, dtype=np.int32)
            ).type(torch.int32).unsqueeze(0)
            input_ids.append(input_id)
            input_lengths.append(input_id.shape[-1])
        max_length = max(input_lengths)
        # do padding, should move outside the profiling to prevent the overhead
        for i in range(len(input_ids)):
            pad_size = max_length - input_lengths[i]

            pad = torch.ones([1, pad_size]).type(torch.int32) * pad_id
            input_ids[i] = torch.cat(
                [torch.IntTensor(input_ids[i]), pad], axis=-1)
        input_ids = torch.cat(input_ids, axis=0).cuda()
        input_lengths = torch.IntTensor(input_lengths).type(torch.int32).cuda()
        return input_ids, input_lengths
    
    def generate(
        self,
        input_ids: torch.Tensor,
        input_lengths: torch.Tensor,
        sampling_config: SamplingConfig,
        max_new_tokens: int,
        runtime_rank: int = 0,
    ):
        max_input_length = torch.max(input_lengths).item()
        max_new_tokens = min(
            max_new_tokens,
            self.global_max_output_length - max_input_length
        )
        # setup batch_size, max_input_length, max_output_len
        self.setup(
            batch_size=input_lengths.size(0),
            max_input_length=max_input_length,
            max_new_tokens=max_new_tokens
        )
        output_ids, end_step = self.decode(
            input_ids, input_lengths, sampling_config
        )
        with torch.no_grad():
            torch.cuda.synchronize()
            if runtime_rank == 0:
                outputs = output_ids[:, 0, :max_input_length + end_step]
                return outputs
    
    def chat(
        self,
        tokenizer,
        sampling_config: SamplingConfig,
        input_text: Union[str, List[str]],
        system_text: str = "You are a helpful assistant.",
        history: list = None,
        max_input_length: Union[int, None] = None,
        max_new_tokens: Union[int, None] = None,
        runtime_rank: int = 0,
    ):
        input_ids, input_lengths = self.prepare_for_chat(
            tokenizer=tokenizer,
            input_text=input_text,
            system_text=system_text,
            history=history,
            max_input_length=max_input_length,
        )
        max_input_length = torch.max(input_lengths).item()
        if max_new_tokens is None:
            max_new_tokens = self.global_max_output_length - max_input_length
        else:
            max_new_tokens = min(
                max_new_tokens,
                self.global_max_output_length - max_input_length
            )
        # setup batch_size, max_input_length, max_output_len
        self.setup(
            batch_size=input_lengths.size(0),
            max_input_length=max_input_length,
            max_new_tokens=max_new_tokens
        )
        output_ids, end_step = self.decode(
            input_ids, input_lengths, sampling_config
        )
        with torch.no_grad():
            torch.cuda.synchronize()
            if runtime_rank == 0:
                output_texts = [
                    tokenizer.decode(
                        output_ids[i, 0, input_lengths[i]: input_lengths[i] + end_step],
                        skip_special_tokens=True
                    )
                    for i in range(output_ids.size(0))
                ]
                return output_texts

    def chat_stream(
        self,
        tokenizer,
        sampling_config: SamplingConfig,
        input_text: Union[str, List[str]],
        system_text: str = "You are a helpful assistant.",
        history: list = None,
        max_input_length: Union[int, None] = None,
        max_new_tokens: Union[int, None] = None,
        runtime_rank: int = 0,
    ):
        input_ids, input_lengths = self.prepare_for_chat(
            tokenizer=tokenizer,
            input_text=input_text,
            system_text=system_text,
            history=history,
            max_input_length=max_input_length,
        )
        max_input_length = torch.max(input_lengths).item()
        # setup batch_size, max_input_length, max_output_len
        if max_new_tokens is None:
            max_new_tokens = self.global_max_output_length - max_input_length
        else:
            max_new_tokens = min(
                max_new_tokens,
                self.global_max_output_length - max_input_length
            )
        self.setup(
            batch_size=input_lengths.size(0),
            max_input_length=max_input_length,
            max_new_tokens=max_new_tokens
        )
        with torch.no_grad():
            for (output_ids, end_step) in self.steam_decode(
                input_ids, input_lengths, sampling_config
            ):
                torch.cuda.synchronize()
                if runtime_rank == 0:
                    output_texts = [
                    tokenizer.decode(
                        output_ids[i, 0, input_lengths[i]: input_lengths[i] + end_step],
                            skip_special_tokens=True
                        ) 
                        for i in range(output_ids.size(0))
                    ]
                    yield output_texts


def parse_arguments():
    parser = argparse.ArgumentParser()
    parser.add_argument('--max_new_tokens', type=int, default=200)
    parser.add_argument('--log_level', type=str, default='error')
    parser.add_argument(
        '--engine_dir',
        type=str,
        default=default_config.engine_dir,
    )
    parser.add_argument(
        '--tokenizer_dir',
        type=str,
        default=default_config.tokenizer_dir,
        help="Directory containing the tokenizer.model."
    )
    default_text = "<|im_start|>system\nYou are a helpful assistant.<|im_end|>\n<|im_start|>user\n你好，请问你叫什么？<|im_end|>\n<|im_start|>assistant\n"
    parser.add_argument(
        '--input_text',
        type=str,
        # default='Born in north-east France, Soyer trained as a'
        default=default_text
    )
    parser.add_argument(
        '--input_tokens',
        dest='input_file',
        type=str,
        help=
        'CSV or Numpy file containing tokenized input. Alternative to text input.',
        default=None)
    parser.add_argument(
        '--output_csv',
        type=str,
        help='CSV file where the tokenized output is stored.',
        default=None
    )
    parser.add_argument(
        '--output_npy',
        type=str,
        help='Numpy file where the tokenized output is stored.',
        default=None
    )
    parser.add_argument(
        '--num_beams',
        type=int,
        help="Use beam search if num_beams >1",
        default=1
    )
    return parser.parse_args()


def get_model(tokenizer_dir, engine_dir, log_level='error'):
    # --load the tokenizer and engine #
    tensorrt_llm.logger.set_level(log_level)
    tokenizer = AutoTokenizer.from_pretrained(
        tokenizer_dir,
        legacy=False,
        trust_remote_code=True,
    )
    config_path = os.path.join(engine_dir, 'config.json')
    with open(config_path, 'r') as f:
        config = json.load(f)
    gen_config_path = os.path.join(tokenizer_dir, 'generation_config.json')
    with open(gen_config_path, 'r') as f:
        gen_config = json.load(f)
    top_k = gen_config['top_k']
    top_p = gen_config['top_p']
    chat_format = gen_config['chat_format']
    if chat_format == "raw":
        eos_token_id = gen_config['eos_token_id']
        pad_token_id = gen_config['pad_token_id']
    elif chat_format == "chatml":
        pad_token_id = eos_token_id = tokenizer.im_end_id
    else:
        raise Exception("unkown chat format ", chat_format)

    use_gpt_attention_plugin = config['plugin_config']['gpt_attention_plugin']
    remove_input_padding = config['plugin_config']['remove_input_padding']
    dtype = config['builder_config']['precision']
    world_size = config['builder_config']['tensor_parallel']
    assert world_size == tensorrt_llm.mpi_world_size(), \
        f'Engine world size ({world_size}) != Runtime world size ({tensorrt_llm.mpi_world_size()})'
    num_heads = config['builder_config']['num_heads'] // world_size
    hidden_size = config['builder_config']['hidden_size'] // world_size
    vocab_size = config['builder_config']['vocab_size']
    num_layers = config['builder_config']['num_layers']
    multi_query_mode = config['builder_config']['multi_query_mode']

    runtime_rank = tensorrt_llm.mpi_rank()
    runtime_mapping = tensorrt_llm.Mapping(world_size, runtime_rank)
    torch.cuda.set_device(runtime_rank % runtime_mapping.gpus_per_node)

    model_config = ModelConfig(
        num_heads=num_heads,
        hidden_size=hidden_size,
        vocab_size=vocab_size,
        num_layers=num_layers,
        gpt_attention_plugin=use_gpt_attention_plugin,
        multi_query_mode=multi_query_mode,
        remove_input_padding=remove_input_padding
    )
    sampling_config = SamplingConfig(
        end_id=eos_token_id,
        pad_id=pad_token_id,
        num_beams=1,
        top_k = top_k,
        top_p = top_p,
    )

    engine_name = get_engine_name('qwen', dtype, world_size, runtime_rank)
    serialize_path = os.path.join(engine_dir, engine_name)
    print(f'Loading engine from {serialize_path}')
    return (
        model_config, sampling_config, runtime_mapping, runtime_rank,
        serialize_path, remove_input_padding, 
        tokenizer, eos_token_id, pad_token_id
    )


def generate(
    max_new_tokens: int,
    log_level: str = 'error',
    engine_dir: str = 'qwen_outputs',
    input_text: str = 'Born in north-east France, Soyer trained as a',
    input_file: str = None,
    output_csv: str = None,
    output_npy: str = None,
    tokenizer_dir: str = None,
    num_beams: int = 1,
):
    (
        model_config, sampling_config, runtime_mapping, runtime_rank,
        serialize_path, remove_input_padding, 
        tokenizer, eos_token_id, pad_token_id
    ) = get_model(tokenizer_dir, engine_dir, log_level)
    with open(serialize_path, 'rb') as f:
        engine_buffer = f.read()
    decoder = QWenForCausalLMGenerationSession(
        model_config,
        engine_buffer,
        runtime_mapping,
    )

    input_tokens = []
    if input_file is None:
        input_tokens.append(
            tokenizer.encode(input_text, add_special_tokens=False))
    else:
        if input_file.endswith('.csv'):
            with open(input_file, 'r') as csv_file:
                csv_reader = csv.reader(csv_file, delimiter=',')
                for line in csv_reader:
                    input_tokens.append(np.array(line, dtype='int32'))
        elif input_file.endswith('.npy'):
            inputs = np.load(input_file)
            for row in inputs:
                row = row[row != eos_token_id]
                input_tokens.append(row)
        else:
            print('Input file format not supported.')
            raise SystemExit

    input_ids = None
    input_lengths = None
    if input_file is None:
        input_ids = torch.cuda.IntTensor(input_tokens)
        input_lengths = torch.cuda.IntTensor([input_ids.size(1)])
    else:
        input_lengths = torch.cuda.IntTensor([len(x) for x in input_tokens])
        if remove_input_padding:
            input_ids = np.concatenate(input_tokens)
            input_ids = torch.cuda.IntTensor(input_ids).unsqueeze(0)
        else:
            input_ids = torch.nested.to_padded_tensor(
                torch.nested.nested_tensor(input_tokens, dtype=torch.int32),
                eos_token_id).cuda()

    max_input_length = torch.max(input_lengths).item()
    max_new_tokens = min(
        max_new_tokens,
        default_config.max_input_len + default_config.max_new_tokens - max_input_length
    )
    decoder.setup(
        batch_size=input_lengths.size(0),
        max_input_length=max_input_length,
        max_new_tokens=max_new_tokens
    )

    output_ids, end_step = decoder.decode(input_ids, input_lengths, sampling_config)
    torch.cuda.synchronize()

    if runtime_rank == 0:
        if output_csv is None and output_npy is None:
            for b in range(input_lengths.size(0)):
                inputs = input_tokens[b]
                input_text = tokenizer.decode(inputs)
                print(f'Input: \"{input_text}\"')
                if num_beams <= 1:
                    output_begin = max_input_length
                    output_end = max_input_length + end_step
                    outputs = output_ids[b][0][output_begin: output_end].tolist()
                    output_text = tokenizer.decode(outputs)
                    # outputs = output_ids[b][0].tolist()
                    # output_text = _decode_chatml(
                    #     outputs,
                    #     stop_words=[],
                    #     eod_token_ids=[tokenizer.im_start_id, tokenizer.im_end_id],
                    #     tokenizer=tokenizer,
                    #     raw_text_len=len(input_text),
                    #     context_length=len(inputs)
                    # )
                    print(f'Output: \"{output_text}\"')
                else:
                    for beam in range(num_beams):
                        output_begin = input_lengths[b]
                        output_end = input_lengths[b] + end_step
                        outputs = output_ids[b][beam][
                            output_begin:output_end].tolist()
                        output_text = tokenizer.decode(outputs)
                        print(f'Output: \"{output_text}\"')

        output_ids = output_ids.reshape((-1, output_ids.size(2)))

        if output_csv is not None:
            output_file = Path(output_csv)
            output_file.parent.mkdir(exist_ok=True, parents=True)
            outputs = output_ids.tolist()
            with open(output_file, 'w') as csv_file:
                writer = csv.writer(csv_file, delimiter=',')
                writer.writerows(outputs)

        if output_npy is not None:
            output_file = Path(output_npy)
            output_file.parent.mkdir(exist_ok=True, parents=True)
            outputs = np.array(output_ids.cpu().contiguous(), dtype='int32')
            np.save(output_file, outputs)
    return


if __name__ == '__main__':
    args = parse_arguments()
    generate(**vars(args))
