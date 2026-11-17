#!/usr/bin/env bash
# Reproduce the mixed-reel defect with the pipeline's pre-fix commands:
# build_trim_command (re-encode video, no audio handling) then
# build_assembly_command (concat demuxer). Clip "a" and "c" have a tone,
# clip "b" is video only, as on a day the microphone dropped out.
set -euo pipefail
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
cd "$work"
for n in a b c; do
  if [ "$n" = b ]; then
    ffmpeg -v error -y -f lavfi -i testsrc=size=320x240:rate=30 -t 20 \
      -c:v libx264 -pix_fmt yuv420p "seg_$n.mp4"
  else
    ffmpeg -v error -y -f lavfi -i testsrc=size=320x240:rate=30 \
      -f lavfi -i sine=frequency=440:sample_rate=48000 -t 20 \
      -c:v libx264 -pix_fmt yuv420p -c:a aac -ac 1 "seg_$n.mp4"
  fi
  ffmpeg -v error -y -ss 2.5 -i "seg_$n.mp4" -t 15 -c:v libx264 "clip_$n.mp4"
done
for order in "a b c" "b a c"; do
  : > concat.txt
  for x in $order; do echo "file '$work/clip_$x.mp4'" >> concat.txt; done
  ffmpeg -v error -y -f concat -safe 0 -i concat.txt -s 1280x720 -c:v libx264 reel.mp4
  echo "clip order: $order"
  echo "  streams (type,duration):"
  ffprobe -v error -show_entries stream=codec_type,duration -of csv=p=0 reel.mp4 | sed 's/^/    /'
  ffprobe -v error -select_streams a -show_entries packet=pts_time -of csv=p=0 reel.mp4 \
    | awk '{ if (p != "" && $1 - p > 0.5) print "  audio packet gap from " p " s to " $1 " s"; p = $1 }'
done
