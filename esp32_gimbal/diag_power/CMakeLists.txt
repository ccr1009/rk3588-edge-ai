cmake_minimum_required(VERSION 3.16)

# 舵机诊断固件（单独工程，不覆盖主固件 esp32_gimbal）
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(servo_diag)
