CXX ?= g++
MOC ?= /usr/lib/qt6/moc
PREFIX ?= $(HOME)/.local
PLUGINDIR ?= $(PREFIX)/lib/qt6/plugins/kf6/kio

INCLUDES = \
	-I. \
	-I/usr/lib/qt6/mkspecs/linux-g++ \
	-I/usr/include/qt6 \
	-I/usr/include/qt6/QtCore \
	-I/usr/include/KF6/KIOCore \
	-I/usr/include/KF6/KIO \
	-I/usr/include/KF6/KCoreAddons \
	-I/usr/include/KF6/KConfigCore \
	-I/usr/include/KF6/KService

CXXFLAGS ?= -std=c++20 -fPIC -O2 -DQT_PLUGIN -DQT_CORE_LIB -DQT_NO_KEYWORDS
LDFLAGS ?= -shared
LIBS ?= -lKF6KIOCore -lKF6CoreAddons -lQt6Core -lQt6Network -lQt6DBus

.PHONY: all clean install

all: kio_fastonedrive.so

kio_fastonedrive.so: kio_fastonedrive.cpp kio_fastonedrive.json
	$(MOC) $(INCLUDES) kio_fastonedrive.cpp -o kio_fastonedrive.moc
	$(CXX) $(CXXFLAGS) $(INCLUDES) $(LDFLAGS) kio_fastonedrive.cpp -o kio_fastonedrive.so $(LIBS)

install: kio_fastonedrive.so
	mkdir -p "$(PLUGINDIR)"
	cp -f kio_fastonedrive.so "$(PLUGINDIR)/.kio_fastonedrive.so.tmp"
	mv -f "$(PLUGINDIR)/.kio_fastonedrive.so.tmp" "$(PLUGINDIR)/kio_fastonedrive.so"

clean:
	rm -f kio_fastonedrive.so kio_fastonedrive.moc
