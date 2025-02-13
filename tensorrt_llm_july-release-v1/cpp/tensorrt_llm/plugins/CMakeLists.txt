#
# SPDX-FileCopyrightText: Copyright (c) 1993-2022 NVIDIA CORPORATION &
# AFFILIATES. All rights reserved. SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not
# use this file except in compliance with the License. You may obtain a copy of
# the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations under
# the License.
#

set(PLUGIN_TARGET_NAME nvinfer_plugin)
set(PLUGIN_SHARED_TARGET ${PLUGIN_TARGET_NAME})

set(TARGET_DIR ${CMAKE_CURRENT_SOURCE_DIR})
set(PLUGIN_EXPORT_MAP ${TARGET_DIR}/exports.map)

if(${CMAKE_BUILD_TYPE} MATCHES "Debug")
  set(CMAKE_CXX_FLAGS "${CMAKE_CXX_FLAGS} -g")
endif()

set(PLUGIN_SOURCES)
set(PLUGIN_CU_SOURCES)

set(PLUGIN_LISTS
    bertAttentionPlugin
    gptAttentionCommon
    gptAttentionPlugin
    ibGptAttentionPlugin
    identityPlugin
    layernormPlugin
    layernormQuantizationPlugin
    rmsnormPlugin
    rmsnormQuantizationPlugin
    gemmPlugin
    smoothQuantGemmPlugin
    quantizePerTokenPlugin
    quantizeTensorPlugin
    ncclPlugin
    weightOnlyQuantMatmulPlugin
    lookupPlugin)

foreach(PLUGIN_ITER ${PLUGIN_LISTS})
  include_directories(${PLUGIN_ITER})
  add_subdirectory(${PLUGIN_ITER})
endforeach(PLUGIN_ITER)

include_directories(common)
add_subdirectory(common)

# Set gencodes
list(APPEND PLUGIN_SOURCES "${PLUGIN_CU_SOURCES}")

list(APPEND PLUGIN_SOURCES "${CMAKE_CURRENT_SOURCE_DIR}/api/InferPlugin.cpp")

# ################################# SHARED LIBRARY
# ##############################################################################

add_library(${PLUGIN_SHARED_TARGET} SHARED ${PLUGIN_SOURCES})

target_include_directories(
  ${PLUGIN_SHARED_TARGET}
  PUBLIC ${CUDA_INSTALL_DIR}/include ${MPI_INCLUDE_PATH}
  PRIVATE ${TARGET_DIR})

if(CUDA_VERSION VERSION_LESS 11.0)
  target_include_directories(${PLUGIN_SHARED_TARGET} PUBLIC ${CUB_ROOT_DIR})
endif()

set_target_properties(
  ${PLUGIN_SHARED_TARGET}
  PROPERTIES CXX_STANDARD "17"
             CXX_STANDARD_REQUIRED "YES"
             CXX_EXTENSIONS "NO"
             ARCHIVE_OUTPUT_DIRECTORY "${TRT_OUT_DIR}"
             LIBRARY_OUTPUT_DIRECTORY "${TRT_OUT_DIR}"
             RUNTIME_OUTPUT_DIRECTORY "${TRT_OUT_DIR}")

set_target_properties(
  ${PLUGIN_SHARED_TARGET}
  PROPERTIES
    LINK_FLAGS
    "-Wl,--exclude-libs,ALL -Wl,--version-script=${PLUGIN_EXPORT_MAP} -Wl,--no-undefined"
)

set_target_properties(
  ${PLUGIN_SHARED_TARGET} PROPERTIES VERSION ${TRT_VERSION} SOVERSION
                                                            ${TRT_SOVERSION})

set_property(TARGET ${PLUGIN_SHARED_TARGET} PROPERTY CUDA_STANDARD 17)

target_link_libraries(
  ${PLUGIN_SHARED_TARGET}
  ${CUBLAS_LIB}
  ${CUBLASLT_LIB}
  ${CUDART_LIB}
  ${CUDNN_LIB}
  nvinfer
  ${CUDA_DRV_LIB}
  ${CMAKE_DL_LIBS}
  ${MPI_LIBRARIES}
  ${NCCL_LIB}
  ${STATIC_TARGET})
