# A Raspberry Pi OS trixie stand-in for running the real setup-device.sh end
# to end. Debian trixie is what Raspberry Pi OS trixie is built on; what it
# lacks (rpicam-apps, raspi-config, a boot partition, systemd as PID 1) is
# stubbed in stubs/ or seeded by run.sh. The apt packages preflight imports are
# installed for real here, so the provisioned venv is genuinely checked.
FROM debian:trixie
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      git ca-certificates python3 python3-spidev python3-gpiozero alsa-utils ffmpeg \
      gawk procps \
 && rm -rf /var/lib/apt/lists/*
# The Raspberry Pi OS default user, in the groups its image gives it.
RUN groupadd -f spi && groupadd -f gpio && groupadd -f i2c \
 && useradd -m -s /bin/bash -G video,audio,spi,gpio,i2c pi
COPY stubs/ /opt/stubs/
RUN chmod +x /opt/stubs/*
ENV PATH=/opt/stubs:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
