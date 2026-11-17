# A Raspberry Pi OS trixie stand-in for running the real setup-device.sh end
# to end. Debian trixie is what Raspberry Pi OS trixie is built on; what it
# lacks (rpicam-apps, a boot partition, systemd as PID 1) is stubbed in stubs/
# or seeded by run.sh. The apt packages preflight imports are installed for
# real here, so the provisioned venv is genuinely checked.
FROM debian:trixie
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      git ca-certificates python3 python3-spidev python3-gpiozero alsa-utils ffmpeg \
      gawk procps \
 && rm -rf /var/lib/apt/lists/*
# The Raspberry Pi OS default user, in the groups its image gives it.
RUN groupadd -f spi && groupadd -f gpio && groupadd -f i2c \
 && useradd -m -s /bin/bash -G video,audio,spi,gpio,i2c pi
# Raspberry Pi OS's real raspi-config, from the trixie branch at a pinned
# commit (package 20261026+2) and checksum-verified, so step 3 enables SPI and
# I2C exactly as a device does, editing the seeded /boot/firmware/config.txt.
# Only what it does to the running kernel - dtparam and modprobe - is stubbed.
# /etc/modules and /etc/modprobe.d are what kmod gives a real image.
ADD https://raw.githubusercontent.com/RPi-Distro/raspi-config/22cea75ecbcb8e6f6e2e80a7f565d6cfe154bc02/raspi-config /usr/bin/raspi-config
RUN echo "b36cfe07286586af029f815dbd3de50529e54061285a64917c935a5cd82651eb  /usr/bin/raspi-config" | sha256sum -c - \
 && chmod 0755 /usr/bin/raspi-config \
 && mkdir -p /etc/modprobe.d \
 && printf '# /etc/modules: kernel modules to load at boot time.\n' > /etc/modules
COPY stubs/ /opt/stubs/
RUN chmod +x /opt/stubs/*
ENV PATH=/opt/stubs:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
