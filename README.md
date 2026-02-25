# Mamo Media Player

Mamo is a simple GTK4/Adwaita based media player written in Python, focusing on local music playback - it's originally based on Namo and leans heavility into the Muine interface.

![image](mamo1.png)
![image](mamo2.png)

## Features

*   Playback of local audio files supported by GStreamer
*   Metadata display (Artist, Album, Title, Track Number)
*   Album artwork display (uses embedded artwork or images in folders) 
*   Window color tint based on currently playing song artwork (optional)
*   Playlist management (adding, removing, saving, loading).
*   Standard media player controls (Play/Pause, Next/Previous, Seek)
*   MPRIS support
*   GTK4 and Libadwaita
*   Okayish waveform
*   Muine like interface
*   Muine like album browser


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


Mamo is originally based on [Namo](https://github.com/hardcoeur/Namo)
