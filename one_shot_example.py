from qast import discover, cast

# Discover devices on the network, devices are cached
print("Discovering...")
devices = discover()
for i, d in enumerate(devices):
    print(f"  [{i}] {d.name} ({d.protocol})")

# Select by device object
print("Casting YouTube video")
cast("https://www.youtube.com/watch?v=PwylW_sUfQY", device=devices[0], duration=15)

# Cast a file (blocks until done or Ctrl+C), use string to select device
print("Casting file")
cast("out.mp4", device="roku")

# Cast your screen, use raw device index
print("Casting screen")
cast("screen", device=0, duration=10)

