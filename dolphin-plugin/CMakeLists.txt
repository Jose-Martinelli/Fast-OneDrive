cmake_minimum_required(VERSION 3.16)
project(fastonedrive_dolphin VERSION 0.2.0 LANGUAGES CXX)

# Plugins do Dolphin para a pasta FUSE (~/OneDrive):
#  - fastonedriveoverlay  (kf6/overlayicon)     emblemas de estado como no Explorer
#  - fastonedriveactions  (kf6/kfileitemaction) submenu "OneDrive" no botão direito
# Ambos só actuam em caminhos montados com o tipo fuse.fastonedrive.

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_AUTOMOC ON)
set(CMAKE_INCLUDE_CURRENT_DIR ON)
# Sem isto o kcoreaddons_add_plugin gera plugins estáticos e não instala nada.
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

find_package(Qt6 REQUIRED COMPONENTS Core DBus Gui Widgets)
find_package(KF6CoreAddons REQUIRED)
find_package(KF6KIO REQUIRED)

kcoreaddons_add_plugin(fastonedriveoverlay
    SOURCES fastonedriveoverlay.cpp
    INSTALL_NAMESPACE "kf6/overlayicon"
)
set_target_properties(fastonedriveoverlay PROPERTIES PREFIX "")
target_link_libraries(fastonedriveoverlay
    PRIVATE
        Qt6::Core
        Qt6::DBus
        KF6::KIOCore
)

kcoreaddons_add_plugin(fastonedriveactions
    SOURCES fastonedriveactions.cpp
    INSTALL_NAMESPACE "kf6/kfileitemaction"
)
set_target_properties(fastonedriveactions PROPERTIES PREFIX "")
target_link_libraries(fastonedriveactions
    PRIVATE
        Qt6::Core
        Qt6::Gui
        Qt6::Widgets
        KF6::CoreAddons
        KF6::KIOCore
        KF6::KIOWidgets
)
