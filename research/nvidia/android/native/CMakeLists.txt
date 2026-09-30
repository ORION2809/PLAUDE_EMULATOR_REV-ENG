# speechbench native layer (docs/nvidia-speech.md): three engine libraries, a CLI
# and the JNI bridge, built against prebuilt NeMo-Speech.cpp, whisper.cpp and
# sherpa-onnx. Configure through research/nvidia/android/build-native.sh.
#
# Layout of the result (one directory, copied to the device or into jniLibs):
#   libsb_nemo.so     NVIDIA C ABI engine  -> NeMo-Speech.cpp's own shared libs (libggml*.so ...)
#   libsb_whisper.so  whisper.cpp engine with whisper + ITS ggml linked statically and
#                     hidden, so the two ggml builds never meet in one process
#   libsb_sherpa.so   sherpa-onnx engine   -> libsherpa-onnx-c-api.so, libonnxruntime.so
#   libspeechbench.so JNI bridge, speechbench (CLI)
cmake_minimum_required(VERSION 3.22)
project(speechbench CXX)
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_VISIBILITY_PRESET hidden)
set(CMAKE_VISIBILITY_INLINES_HIDDEN ON)

set(SB_NSC_ROOT "" CACHE PATH "NeMo-Speech.cpp source (include/)")
set(SB_NSC_LIBDIR "" CACHE PATH "directory with libnemo_speech_asr_c and its shared deps")
set(SB_WCPP_ROOT "" CACHE PATH "whisper.cpp source")
set(SB_WCPP_BUILD "" CACHE PATH "whisper.cpp static build dir (BUILD_SHARED_LIBS=OFF)")
set(SB_SHERPA_INCLUDE "" CACHE PATH "directory holding sherpa-onnx/c-api/c-api.h")
set(SB_SHERPA_LIBDIR "" CACHE PATH "directory with libsherpa-onnx-c-api and libonnxruntime")
option(SB_WITH_SHERPA "build the sherpa-onnx engine" ON)
option(SB_WITH_JNI "build the JNI bridge" ON)

if(APPLE)
    set(SB_RPATH "@loader_path")
    set(SB_HIDE_STATIC "")
else()
    set(SB_RPATH "$ORIGIN")
    set(SB_HIDE_STATIC "-Wl,--exclude-libs,ALL")
endif()
set(CMAKE_BUILD_RPATH "${SB_RPATH}")
set(CMAKE_INSTALL_RPATH "${SB_RPATH}")

# --- NVIDIA ---------------------------------------------------------------------------
find_library(SB_NSC_C NAMES nemo_speech_asr_c PATHS "${SB_NSC_LIBDIR}" NO_DEFAULT_PATH NO_CMAKE_FIND_ROOT_PATH REQUIRED)
add_library(sb_nemo SHARED engine_nemo.cpp)
target_include_directories(sb_nemo PRIVATE "${SB_NSC_ROOT}/include")
target_link_libraries(sb_nemo PRIVATE "${SB_NSC_C}")
if(NOT APPLE)
    target_link_options(sb_nemo PRIVATE "-Wl,-rpath-link,${SB_NSC_LIBDIR}")
endif()

# --- whisper.cpp (static, hidden) -----------------------------------------------------
set(_w "${SB_WCPP_BUILD}")
set(SB_WCPP_LIBS "${_w}/src/libwhisper.a" "${_w}/ggml/src/libggml.a" "${_w}/ggml/src/libggml-cpu.a" "${_w}/ggml/src/libggml-base.a")
add_library(sb_whisper SHARED engine_whisper.cpp)
target_include_directories(sb_whisper PRIVATE "${SB_WCPP_ROOT}/include" "${SB_WCPP_ROOT}/ggml/include")
if(APPLE)
    set(SB_WCPP_LIBS "${_w}/src/libwhisper.a" "${_w}/ggml/src/libggml.a" "${_w}/ggml/src/ggml-metal/libggml-metal.a"
        "${_w}/ggml/src/ggml-blas/libggml-blas.a" "${_w}/ggml/src/libggml-cpu.a" "${_w}/ggml/src/libggml-base.a")
    target_link_libraries(sb_whisper PRIVATE ${SB_WCPP_LIBS} "-framework Foundation" "-framework Metal"
                          "-framework MetalKit" "-framework Accelerate")
else()
    target_link_libraries(sb_whisper PRIVATE ${SB_WCPP_LIBS} ${SB_HIDE_STATIC} m)
endif()

# --- sherpa-onnx ----------------------------------------------------------------------
if(SB_WITH_SHERPA)
    find_library(SB_SHERPA_C NAMES sherpa-onnx-c-api PATHS "${SB_SHERPA_LIBDIR}" NO_DEFAULT_PATH NO_CMAKE_FIND_ROOT_PATH REQUIRED)
    add_library(sb_sherpa SHARED engine_sherpa.cpp)
    target_include_directories(sb_sherpa PRIVATE "${SB_SHERPA_INCLUDE}")
    target_link_libraries(sb_sherpa PRIVATE "${SB_SHERPA_C}")
    if(NOT APPLE)
        target_link_options(sb_sherpa PRIVATE "-Wl,-rpath-link,${SB_SHERPA_LIBDIR}")
    endif()
endif()

# --- CLI and JNI ----------------------------------------------------------------------
add_executable(speechbench cli.cpp)
target_link_libraries(speechbench PRIVATE sb_nemo sb_whisper)
if(SB_WITH_SHERPA)
    target_link_libraries(speechbench PRIVATE sb_sherpa)
else()
    target_compile_definitions(speechbench PRIVATE SB_NO_SHERPA)
endif()
if(ANDROID)
    target_link_options(speechbench PRIVATE "-Wl,-rpath-link,${SB_NSC_LIBDIR}" "-Wl,-rpath-link,${SB_SHERPA_LIBDIR}")
endif()

if(SB_WITH_JNI AND ANDROID AND SB_WITH_SHERPA)
    add_library(speechbench_jni SHARED jni.cpp)
    set_target_properties(speechbench_jni PROPERTIES OUTPUT_NAME speechbench)
    target_link_libraries(speechbench_jni PRIVATE sb_nemo sb_whisper sb_sherpa)
    target_link_options(speechbench_jni PRIVATE "-Wl,-rpath-link,${SB_NSC_LIBDIR}" "-Wl,-rpath-link,${SB_SHERPA_LIBDIR}")
endif()
