# A Raspberry Pi OS trixie stand-in for the de-rooted recorder: the base
# provisioning harness (docs/superpowers/evidence/provisioning-led-spi-audio/)
# plus a real D-Bus system bus, polkitd and NetworkManager, so the polkit rule
# and the unprivileged nmcli calls are exercised against the real daemons, and
# systemd for systemd-analyze. systemd is not PID 1 here; run.sh starts the
# three daemons by hand.
FROM debian:trixie
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      git ca-certificates python3 python3-spidev python3-gpiozero alsa-utils ffmpeg \
      gawk procps network-manager polkitd dbus systemd \
 && rm -rf /var/lib/apt/lists/*
# Raspberry Pi OS creates these at image build (pi-gen stage2 01-sys-tweaks)
# and puts the first user in them.
RUN groupadd -f -r spi && groupadd -f -r gpio && groupadd -f -r i2c \
 && useradd -m -s /bin/bash -G video,audio,spi,gpio,i2c,netdev pi
COPY stubs/ /opt/stubs/
RUN chmod +x /opt/stubs/*
ENV PATH=/opt/stubs:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
