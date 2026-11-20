cmake_minimum_required(VERSION 3.16)
project(kio_fastonedrive VERSION 0.1.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_AUTOMOC ON)
set(CMAKE_INCLUDE_CURRENT_DIR ON)
set(BUILD_SHARED_LIBS ON)

find_package(ECM 5.38 QUIET NO_MODULE)
if(ECM_FOUND)
	list(APPEND CMAKE_MODULE_PATH ${ECM_MODULE_PATH})
	include(KDEInstallDirs)
	include(KDECMakeSettings)
else()
	include(GNUInstallDirs)
	set(KDE_INSTALL_PLUGINDIR "${CMAKE_INSTALL_LIBDIR}/qt6/plugins")
	set(CMAKE_LIBRARY_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
endif()

find_package(Qt6 REQUIRED COMPONENTS Core)
find_package(KF6CoreAddons REQUIRED)
find_package(KF6KIO REQUIRED)

kcoreaddons_add_plugin(kio_fastonedrive
	SOURCES kio_fastonedrive.cpp
	INSTALL_NAMESPACE "kf6/kio"
)
set_source_files_properties(kio_fastonedrive.cpp PROPERTIES
	OBJECT_DEPENDS "${CMAKE_CURRENT_SOURCE_DIR}/kio_fastonedrive.json"
)
set_target_properties(kio_fastonedrive PROPERTIES PREFIX "")

target_link_libraries(kio_fastonedrive
	PRIVATE
		Qt6::Core
		KF6::KIOCore
		KF6::CoreAddons
)
