# Builds libdshot.so = dshot_pio.c + Raspberry Pi's piolib. Run on the Pi 5: make
PIOLIB = third_party/utils/piolib

libdshot.so: dshot_pio.c | $(PIOLIB)
	gcc -O2 -shared -fPIC -DLIBRARY_BUILD=1 -I$(PIOLIB)/include -o $@ \
	    dshot_pio.c $(PIOLIB)/piolib.c $(PIOLIB)/library_piochips.c $(PIOLIB)/pio_rp1.c -lpthread

$(PIOLIB):
	git clone --depth 1 https://github.com/raspberrypi/utils third_party/utils

clean:
	rm -f libdshot.so
