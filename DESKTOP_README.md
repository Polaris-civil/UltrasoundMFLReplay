# Native synchronized replay

Use 启动回放.bat or 启动桌面回放.bat. The default launcher now opens the
native PyQt5 application. It does not start a browser or an HTTP server.

The window is organized as follows:

- the left side contains playback, Track/Gate filters, display settings, and
  current-window information;
- the right side contains ultrasound on top and MFL on the bottom;
- both plots share the joint-aligned relative-distance axis.

Ultrasound Gate markers are vector outlines. Their shape and rotation follow
the existing ultrasound viewer's Gate style mapping; marker size can be entered
as 6-20 px in the left panel (default 8 px). The marker outline/rotation carries
direction information, while color identifies the Gate number.

MFL values are displayed as DAT.value / 1000. The default scale is automatic
per row: P99 * 1.35 from the currently loaded window. Every row shows its
current positive and negative limits in a right-side gutter. The display
panel also provides fixed unified limits when cross-row amplitude comparison is
needed.

The data reader runs in a worker thread. Manual stepping and continuous playback
use a five-block directional buffer queue. For the default 8 m viewport, each
active block retains roughly 12 m on both sides, and the next blocks are loaded
in the background. Rendering is clipped to the visible range so larger buffers
do not add proportional paint cost. MFL downsampling is performed bucket-by-
bucket, retaining bucket endpoints and X/Z extrema without materializing the
whole raw window.

The original DAT, CSV, and ultrasound binary data are not copied or modified.
Paths and joint alignment remain in config.json.

GitHub Release based update checking and the Windows portable-app installer are
documented in [UPDATE_RELEASE.md](UPDATE_RELEASE.md). The release asset contains
only the executable and its `_internal` folder; local configuration and data
stay on the user's computer.
