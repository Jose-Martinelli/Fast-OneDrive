# Compila os plugins do Dolphin sem cmake (o mesmo caminho do worker KIO em kio/Makefile).
CXX ?= g++
MOC ?= /usr/lib/qt6/moc
PREFIX ?= $(HOME)/.local
PLUGINROOT ?= $(PREFIX)/lib/qt6/plugins/kf6

INCLUDES = \
	-I. \
	-I/usr/lib/qt6/mkspecs/linux-g++ \
	-I/usr/include/qt6 \
	-I/usr/include/qt6/QtCore \
	-I/usr/include/qt6/QtGui \
	-I/usr/include/qt6/QtWidgets \
	-I/usr/include/qt6/QtDBus \
	-I/usr/include/KF6/KIOCore \
	-I/usr/include/KF6/KIOWidgets \
	-I/usr/include/KF6/KIO \
	-I/usr/include/KF6/KCoreAddons \
	-I/usr/include/KF6/KConfigCore \
	-I/usr/include/KF6/KService

CXXFLAGS ?= -std=c++20 -fPIC -O2 -DQT_PLUGIN -DQT_CORE_LIB -DQT_GUI_LIB -DQT_WIDGETS_LIB -DQT_DBUS_LIB -DQT_NO_KEYWORDS
LDFLAGS ?= -shared

.PHONY: all clean install

all: fastonedriveoverlay.so fastonedriveactions.so

fastonedriveoverlay.so: fastonedriveoverlay.cpp fastonedriveoverlay.json fastonedrivemounts.h
	$(MOC) $(INCLUDES) fastonedriveoverlay.cpp -o fastonedriveoverlay.moc
	$(CXX) $(CXXFLAGS) $(INCLUDES) $(LDFLAGS) fastonedriveoverlay.cpp -o $@ -lKF6KIOCore -lQt6DBus -lQt6Core

fastonedriveactions.so: fastonedriveactions.cpp fastonedriveactions.json fastonedrivemounts.h
	$(MOC) $(INCLUDES) fastonedriveactions.cpp -o fastonedriveactions.moc
	$(CXX) $(CXXFLAGS) $(INCLUDES) $(LDFLAGS) fastonedriveactions.cpp -o $@ -lKF6KIOWidgets -lKF6KIOCore -lKF6CoreAddons -lQt6Widgets -lQt6Gui -lQt6Core

install: all
	mkdir -p "$(PLUGINROOT)/overlayicon" "$(PLUGINROOT)/kfileitemaction"
	cp -f fastonedriveoverlay.so "$(PLUGINROOT)/overlayicon/.fastonedriveoverlay.so.tmp"
	mv -f "$(PLUGINROOT)/overlayicon/.fastonedriveoverlay.so.tmp" "$(PLUGINROOT)/overlayicon/fastonedriveoverlay.so"
	cp -f fastonedriveactions.so "$(PLUGINROOT)/kfileitemaction/.fastonedriveactions.so.tmp"
	mv -f "$(PLUGINROOT)/kfileitemaction/.fastonedriveactions.so.tmp" "$(PLUGINROOT)/kfileitemaction/fastonedriveactions.so"

clean:
	rm -f *.so *.moc
