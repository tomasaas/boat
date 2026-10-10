# Builds libdshot.so = dshot_pio.c + Raspberry Pi's piolib. Run on the Pi 5: make
# make install    also installs ffmpeg (the camera) and starts app.py at boot (systemd service "boat").
#                 Run as your user, not sudo.
PIOLIB = third_party/utils/piolib

libdshot.so: dshot_pio.c | $(PIOLIB)
	gcc -O2 -shared -fPIC -DLIBRARY_BUILD=1 -I$(PIOLIB)/include -o $@ \
	    dshot_pio.c $(PIOLIB)/piolib.c $(PIOLIB)/library_piochips.c $(PIOLIB)/pio_rp1.c -lpthread

$(PIOLIB):
	git clone --depth 1 https://github.com/raspberrypi/utils third_party/utils

install: libdshot.so | /usr/bin/ffmpeg
	sed -e "s|@USER@|$$(id -un)|" -e "s|@DIR@|$(CURDIR)|g" boat.service | sudo tee /etc/systemd/system/boat.service >/dev/null
	sudo systemctl daemon-reload
	sudo systemctl enable boat
	sudo systemctl restart boat

/usr/bin/ffmpeg:
	sudo apt-get install -y ffmpeg

uninstall:
	-sudo systemctl disable --now boat
	sudo rm -f /etc/systemd/system/boat.service
	sudo systemctl daemon-reload

.PHONY: install uninstall clean

clean:
	rm -f libdshot.so
