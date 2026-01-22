Mamo is based on [Namo](https://github.com/hardcoeur/Namo)

# Mamo Media Player

Mamo is a simple GTK4/Adwaita based media player written in Python, focusing on local music playback.

## Changes from Namo

* Remove bandcamp integration
* Add MPRIS support
* Add wildly inacurate waveform
* Various ui tweaks
* Generally more Muine like

![image](mamo1.png)

## Features

*   Playback of local audio files supported by GStreamer.
*   Metadata display (Artist, Album, Title, Track Number).
*   Album artwork display.
*   Playlist management (adding, removing, saving, loading).
*   Standard media player controls (Play/Pause, Next/Previous, Seek).
*   MPRIS support
*   Modern user interface using GTK4 and Libadwaita.

## Dependencies

**Core Requirements:**

*   Python 3
*   GTK4 & Libadwaita
*   GStreamer (with appropriate plugins)
*   PyGObject (Python bindings for GObject libraries like GTK, GStreamer, etc.)

**Python Libraries:**

*   `mutagen` (for audio metadata)

## Installation

Install the necessary dependencies for your Linux distribution:

### Debian / Ubuntu

```bash
sudo apt update
sudo apt install python3 python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-adw-1 python3-mutagen gir1.2-gst-plugins-base-1.0 gir1.2-gstreamer-1.0 python3-gst-1.0 gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly gstreamer1.0-libav
```

**Note on GStreamer Plugins:** The specific GStreamer plugins listed provide support for a wide range of common audio formats. You might need additional `gstreamer1.0-plugins-*` or `gstreamer1-plugins-*` packages depending on the specific audio codecs you intend to play. `gstreamer1.0-libav` (Debian/Ubuntu) or `gstreamer1-libav` (Fedora) is often required for formats like MP3 and AAC.

## Running Mamo

Navigate to the project's root directory in your terminal and run:

```bash
python3 mamo.py
