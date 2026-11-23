cmake_minimum_required(VERSION 3.16)
project(cix_depth_camera LANGUAGES CXX)

find_package(OrbbecSDK CONFIG REQUIRED)
find_package(Threads REQUIRED)
add_executable(depth_live depth_live.cpp)
target_compile_features(depth_live PRIVATE cxx_std_17)
target_link_libraries(depth_live PRIVATE ob::OrbbecSDK Threads::Threads)
