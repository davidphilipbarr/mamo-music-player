#!/usr/bin/env python3

import sys
import gi

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
gi.require_version('Gst', '1.0')
gi.require_version('GstPbutils', '1.0') 
gi.require_version('GdkPixbuf', '2.0')
gi.require_version('Graphene', '1.0')

import threading
import collections
import os 
import random
import tempfile
import importlib.util 
import html 
import json 
import base64 
import mutagen
import pathlib 
from urllib.parse import urlparse, unquote
import hashlib
from gi.repository import Gtk, Adw, Gio, GLib, GObject, Gst, GstPbutils, GdkPixbuf, Gdk, Pango, Graphene

STYLE_CSS = """
.title-4 {
    font-weight: bold;
    font-size: 10pt;
}

.title-5 {
    font-weight: bold;
    font-size: 12pt;
}

.caption {
    opacity: 0.8;
    font-size: small;
}

.title-3 {
    font-weight: bold;
    font-size: large;
    margin-bottom: 6px;
}

.album-art-image {
/*
    box-shadow: 2px 2px 4px rgba(0, 0, 0, 0.205);
*/
}


.playlist-song-title {
    font-weight: bold;
    font-size: 11pt;
}

.playlist-song-subtitle {
    opacity: 0.7;
    font-size: 9pt;
}

/* Zebra striping for playlist */
.playlist-view row:nth-child(odd) {
    background-color: alpha(currentColor, 0.06);
}
"""





class Song(GObject.Object):
    __gtype_name__ = 'Song'

    title = GObject.Property(type=str, default="Unknown Title")
    uri = GObject.Property(type=str) 
    artist = GObject.Property(type=str, default="Unknown Artist")
    album = GObject.Property(type=str, default="Unknown Album")
    duration = GObject.Property(type=GObject.TYPE_INT64, default=0) 
    album_art_data = GObject.Property(type=GLib.Bytes) 
    is_playing = GObject.Property(type=bool, default=False)

    def __init__(self, uri, title=None, artist=None, album=None, duration=None):
        super().__init__()
        self.uri = uri
        self.title = title if title else "Unknown Title"
        self.artist = artist if artist else "Unknown Artist"
        self.album = album if album else "Unknown Album"
        
        
        self.duration = duration if isinstance(duration, int) and duration >= 0 else 0
        self.waveform_data = None # List of linear amplitude values (0.0 - 1.0)


class Album(GObject.Object):
    __gtype_name__ = 'Album'

    title = GObject.Property(type=str, default="Unknown Album")
    artist = GObject.Property(type=str, default="Unknown Artist")
    art_data = GObject.Property(type=GLib.Bytes)
    folder = GObject.Property(type=str) # The folder containing the album

    def __init__(self, title, artist, folder, art_data=None):
        super().__init__()
        self.title = title
        self.artist = artist
        self.folder = folder
        self.art_data = art_data


class LibraryManager(GObject.Object):
    """
    Manages the persistent library database (at ~/.cache/mamo/library.json).
    Scans the library path in a background thread.
    """
    __gsignals__ = {
        'library-updated': (GObject.SignalFlags.RUN_FIRST, None, ()),
        'scan-started': (GObject.SignalFlags.RUN_FIRST, None, ()),
        'scan-finished': (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, library_path, cache_file):
        super().__init__()
        self.library_path = library_path
        self.cache_file = cache_file
        self.albums = [] # List of Album objects
        self._is_scanning = False
        self._is_loading_cache = False
        threading.Thread(target=self._load_cache_thread, daemon=True).start()

    def _load_cache_thread(self):
        """Background thread to load the library cache."""
        if not os.path.exists(self.cache_file):
            print("LibraryManager: No cache found, starting initial scan.")
            GLib.idle_add(self.start_scan)
            return
        
        self._is_loading_cache = True
        temp_albums = []
        try:
            print(f"LibraryManager: Loading cache from {self.cache_file}")
            with open(self.cache_file, 'r') as f:
                data = json.load(f)
                for item in data:
                    art_data = None
                    if 'art_base64' in item and item['art_base64']:
                        try:
                            raw = base64.b64decode(item['art_base64'])
                            art_data = GLib.Bytes.new(raw)
                        except:
                            pass
                    
                    album = Album(
                        title=item.get('title', 'Unknown Album'),
                        artist=item.get('artist', 'Unknown Artist'),
                        folder=item.get('folder', ''),
                        art_data=art_data
                    )
                    temp_albums.append(album)
            
            def finalize_load():
                self.albums = temp_albums
                self._is_loading_cache = False
                self.emit('library-updated')
                return False

            GLib.idle_add(finalize_load)
        except Exception as e:
            print(f"LibraryManager: Error loading cache: {e}")
            self._is_loading_cache = False

    def _save_cache(self):
        os.makedirs(os.path.dirname(self.cache_file), exist_ok=True)
        try:
            data = []
            for album in self.albums:
                art_base64 = None
                if album.art_data:
                    art_base64 = base64.b64encode(album.art_data.get_data()).decode('utf-8')
                
                data.append({
                    'title': album.title,
                    'artist': album.artist,
                    'folder': album.folder,
                    'art_base64': art_base64
                })
            with open(self.cache_file, 'w') as f:
                json.dump(data, f)
        except Exception as e:
            print(f"LibraryManager: Error saving cache: {e}")

    def start_scan(self):
        if self._is_scanning:
            return
        if not self.library_path or not os.path.exists(self.library_path):
            return

        self._is_scanning = True
        self.emit('scan-started')
        thread = threading.Thread(target=self._scan_worker, daemon=True)
        thread.start()

    def _scan_worker(self):
        print(f"LibraryManager: Starting scan of {self.library_path}")
        found_albums = {} # (artist, title) -> Album

        for root, dirs, files in os.walk(self.library_path):
            audio_files = [f for f in files if not f.startswith('.') and f.lower().endswith(('.mp3', '.flac', '.m4a', '.ogg'))]
            if audio_files:
                first_file = os.path.join(root, audio_files[0])
                try:
                    audio = mutagen.File(first_file, easy=True)
                    if not audio: continue
                    album_title = audio.get('album', ['Unknown Album'])[0]
                    artist = audio.get('artist', ['Unknown Artist'])[0]
                    
                    key = (artist, album_title)
                    if key not in found_albums:
                        art_data = self._find_art_for_folder(root)
                        if not art_data:
                            art_data = self._find_embedded_art(first_file)
                        album = Album(title=album_title, artist=artist, folder=root, art_data=art_data)
                        found_albums[key] = album
                        
                        # Periodically update UI (every 10 albums)
                        if len(found_albums) % 10 == 0:
                            GLib.idle_add(self._on_partial_update, list(found_albums.values()))
                except Exception as e:
                    print(f"LibraryManager: Error scanning {first_file}: {e}")

        final_albums = list(found_albums.values())
        self._save_cache_data(final_albums)
        GLib.idle_add(self._on_scan_complete, final_albums)

    def _on_partial_update(self, albums):
        self.albums = albums
        self.emit('library-updated')
        return False

    def _on_scan_complete(self, albums):
        self.albums = albums
        self._is_scanning = False
        self.emit('library-updated')
        self.emit('scan-finished')

    def _save_cache_data(self, albums):
        os.makedirs(os.path.dirname(self.cache_file), exist_ok=True)
        try:
            data = []
            for album in albums:
                art_base64 = None
                if album.art_data:
                    art_base64 = base64.b64encode(album.art_data.get_data()).decode('utf-8')
                
                data.append({
                    'title': album.title,
                    'artist': album.artist,
                    'folder': album.folder,
                    'art_base64': art_base64
                })
            with open(self.cache_file, 'w') as f:
                json.dump(data, f)
        except Exception as e:
            print(f"LibraryManager: Error saving cache: {e}")

    def _save_cache(self):
        # Wrapper for saving current state
        self._save_cache_data(self.albums)

    def _find_art_for_folder(self, folder):
        cover_filenames = ["cover.jpg", "Cover.jpg", "folder.jpg", "Folder.jpg", "cover.png", "Cover.png"]
        for fn in cover_filenames:
            path = os.path.join(folder, fn)
            if os.path.exists(path):
                try:
                    with open(path, "rb") as f:
                        return GLib.Bytes.new(f.read())
                except:
                    pass
        return None

    def _find_embedded_art(self, filepath):
        """Extracts embedded album art from an audio file."""
        try:
            audio_raw = mutagen.File(filepath)
            if not audio_raw or not audio_raw.tags:
                return None
            
            art_bytes = None
            if isinstance(audio_raw.tags, mutagen.id3.ID3) and 'APIC:' in audio_raw.tags:
                art_bytes = audio_raw.tags['APIC:'].data
            elif isinstance(audio_raw, mutagen.mp4.MP4) and 'covr' in audio_raw.tags and audio_raw.tags['covr']:
                art_bytes = bytes(audio_raw.tags['covr'][0])
            elif hasattr(audio_raw, 'pictures') and audio_raw.pictures:
                art_bytes = audio_raw.pictures[0].data
            
            if art_bytes:
                return GLib.Bytes.new(art_bytes)
        except Exception as e:
            print(f"LibraryManager: Error extracting embedded art from {filepath}: {e}")
        return None


class AlbumBrowser(Adw.Window):
    def __init__(self, parent, library_manager, callback):
        super().__init__(transient_for=parent, modal=True)
        self.set_title("Album Browser")
        self.set_icon_name("multimedia-audio-player")
        self.set_default_size(450, 550)
        self.callback = callback
        self.library_manager = library_manager

        self.albums_store = Gio.ListStore(item_type=Album)
        self.filter_model = Gtk.FilterListModel(model=self.albums_store)
        
        # Sorting: Artist then Title
        self.multi_sorter = Gtk.MultiSorter()
        
        artist_expression = Gtk.PropertyExpression.new(Album, None, "artist")
        artist_sorter = Gtk.StringSorter.new(artist_expression)
        self.multi_sorter.append(artist_sorter)
        
        title_expression = Gtk.PropertyExpression.new(Album, None, "title")
        title_sorter = Gtk.StringSorter.new(title_expression)
        self.multi_sorter.append(title_sorter)
        
        self.sort_model = Gtk.SortListModel(model=self.filter_model, sorter=self.multi_sorter)
        self.selection_model = Gtk.SingleSelection(model=self.sort_model)

        # Connect to library updates
        self.library_manager.connect('library-updated', self._on_library_updated)
        self.library_manager.connect('scan-started', lambda x: self.spinner.start())
        self.library_manager.connect('scan-finished', lambda x: self.spinner.stop())
        self._update_store()

        # UI Setup
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        main_box.set_margin_start(12)
        main_box.set_margin_end(12)
        main_box.set_margin_top(12)
        main_box.set_margin_bottom(12)
        self.set_content(main_box)

        self.search_entry = Gtk.SearchEntry()
        self.search_entry.set_placeholder_text("Search Artist or Album...")
        self.search_entry.connect("search-changed", self._on_search_changed)
        main_box.append(self.search_entry)

        lib_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        main_box.append(lib_box)
        
        self.lib_label = Gtk.Label(label=f"Library: {self.library_manager.library_path}", xalign=0)
        self.lib_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self.lib_label.set_hexpand(True)
        lib_box.append(self.lib_label)

        change_lib_btn = Gtk.Button(label="Change...")
        change_lib_btn.connect("clicked", self._on_change_library_clicked)
        lib_box.append(change_lib_btn)

        re_scan_btn = Gtk.Button(label="Rescan")
        re_scan_btn.connect("clicked", lambda x: self.library_manager.start_scan())
        lib_box.append(re_scan_btn)

        self.spinner = Gtk.Spinner()
        self.spinner.set_margin_start(6)
        lib_box.append(self.spinner)
        if self.library_manager._is_scanning:
            self.spinner.start()

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        main_box.append(scrolled)

        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._on_item_setup)
        factory.connect("bind", self._on_item_bind)

        self.list_view = Gtk.ListView(model=self.selection_model, factory=factory)
        self.list_view.add_css_class("navigation-sidebar")
        self.list_view.connect("activate", lambda lv, pos: self._on_action_clicked(None, "play"))
        scrolled.set_child(self.list_view)

        button_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        button_box.set_halign(Gtk.Align.END)
        main_box.append(button_box)

        cancel_button = Gtk.Button(label="Cancel")
        cancel_button.connect("clicked", lambda x: self.close())
        button_box.append(cancel_button)

        self.queue_button = Gtk.Button(label="Queue")
        self.queue_button.connect("clicked", self._on_action_clicked, "queue")
        self.queue_button.set_sensitive(False)
        button_box.append(self.queue_button)

        self.play_button = Gtk.Button(label="Play")
        self.play_button.add_css_class("suggested-action")
        self.play_button.connect("clicked", self._on_action_clicked, "play")
        self.play_button.set_sensitive(False)
        button_box.append(self.play_button)

        self.selection_model.connect("selection-changed", self._on_selection_changed)
        # Sync initial button state
        self._on_selection_changed(self.selection_model, 0, 0)

    def _update_store(self):
        self.albums_store.remove_all()
        for album in self.library_manager.albums:
            self.albums_store.append(album)

    def _on_library_updated(self, manager):
        self._update_store()

    def _on_item_setup(self, factory, list_item):
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        box.set_margin_start(6)
        box.set_margin_end(6)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        
        image = Gtk.Image()
        image.set_pixel_size(48)
        image.set_from_icon_name("audio-x-generic-symbolic")
        box.append(image)

        details = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        details.set_valign(Gtk.Align.CENTER)
        box.append(details)

        title_label = Gtk.Label(xalign=0)
        title_label.add_css_class("title-4")
        details.append(title_label)

        artist_label = Gtk.Label(xalign=0)
        artist_label.add_css_class("caption")
        details.append(artist_label)

        list_item.set_child(box)

    def _on_item_bind(self, factory, list_item):
        album = list_item.get_item()
        box = list_item.get_child()
        image = box.get_first_child()
        details = image.get_next_sibling()
        title_label = details.get_first_child()
        artist_label = title_label.get_next_sibling()

        title_label.set_label(album.title)
        artist_label.set_label(album.artist)

        if album.art_data:
            try:
                loader = GdkPixbuf.PixbufLoader()
                loader.write(album.art_data.get_data())
                loader.close()
                pixbuf = loader.get_pixbuf()
                scaled = pixbuf.scale_simple(48, 48, GdkPixbuf.InterpType.BILINEAR)
                image.set_from_pixbuf(scaled)
            except Exception:
                image.set_from_icon_name("audio-x-generic-symbolic")
        else:
            image.set_from_icon_name("audio-x-generic-symbolic")

    def _on_search_changed(self, entry):
        search_text = entry.get_text().lower()
        if not search_text:
            self.filter_model.set_filter(None)
            return

        def filter_func(item):
            return (search_text in item.title.lower() or 
                    search_text in item.artist.lower())

        custom_filter = Gtk.CustomFilter.new(filter_func)
        self.filter_model.set_filter(custom_filter)

    def _on_selection_changed(self, selection_model, position, n_items):
        has_selection = selection_model.get_selected_item() is not None
        self.play_button.set_sensitive(has_selection)
        self.queue_button.set_sensitive(has_selection)

    def _on_action_clicked(self, button, action):
        album = self.selection_model.get_selected_item()
        if album:
            # Tell the parent about the library path in case it changed
            if hasattr(self.get_transient_for(), 'library_path'):
                self.get_transient_for().library_path = self.library_manager.library_path
                self.get_transient_for()._save_settings()
            
            self.callback(album, action)
            self.close()

    def _on_change_library_clicked(self, button):
        dialog = Gtk.FileDialog.new()
        dialog.set_title("Select Music Library Folder")
        dialog.select_folder(parent=self, cancellable=None, callback=self._on_library_folder_selected)

    def _on_library_folder_selected(self, dialog, result):
        try:
            folder = dialog.select_folder_finish(result)
            if folder:
                new_path = folder.get_path()
                self.library_manager.library_path = new_path
                self.lib_label.set_label(f"Library: {new_path}")
                
                # Tell parent to save new path
                parent = self.get_transient_for()
                if hasattr(parent, 'library_path'):
                    parent.library_path = new_path
                    parent._save_settings()
                
                self.library_manager.start_scan()
        except Exception as e:
            print(f"Error selecting library folder: {e}")



class WaveformBar(Gtk.DrawingArea):
    """
    A custom widget that renders a pseudo-waveform using vertical bars.
    Supports seeking via click/drag.
    """
    def __init__(self, seek_callback=None):
        super().__init__()
        self.set_content_width(320)
        self.set_content_height(40)
        self.set_draw_func(self.do_draw)
        self.fraction = 0.0
        self.seek_callback = seek_callback
        
        # Display parameters
        self.metric_data = [] # The raw waveform data (linear 0..1)
        self.n_bars = 60 # Default, will be dynamic based on width
        self.amplitudes = [] # Normalized localized amplitudes for display
        self._dirty_resample = True

        # Determine fake initial state (flat line or empty)
        # We start empty until real data comes in
        self.amplitudes = [0.0] * self.n_bars
        
        # Input handling
        self.gesture = Gtk.GestureClick()
        self.gesture.connect("pressed", self._on_pressed)
        # We can also add a motion controller for dragging if desired,
        # but click is sufficient for basic seeking.
        self.add_controller(self.gesture)

    def do_draw(self, area, cr, width, height):
        # Recalculate bars if width changed heavily or just use fixed number
        # Let's fix n_bars to something reasonable or dynamic
        # Dynamic: width / 5
        new_n_bars = max(10, int(width / 4))
        if new_n_bars != self.n_bars:
            self.n_bars = new_n_bars
            self._dirty_resample = True

        if self.metric_data and self._dirty_resample:
            self._resample_data_to_bars()
            self._dirty_resample = False
        elif not self.metric_data and len(self.amplitudes) != self.n_bars:
            self.amplitudes = [0.0] * self.n_bars

        # Bar layout
        bar_width = width / self.n_bars
        gap = 1
        actual_bar_width = max(1, bar_width - gap)
        
        # Optimizing drawing: group by played vs remaining to reduce fill calls
        cr.set_source_rgba(0.208, 0.518, 0.894, 1.0) # Adwaita Blue
        for i in range(self.n_bars):
            if i >= len(self.amplitudes): break
            bar_fraction = i / self.n_bars
            if bar_fraction > self.fraction: break
            
            amp = self.amplitudes[i]
            if amp > 0: amp = max(0.1, amp)
            bar_h = amp * height * 0.9
            y = (height - bar_h) / 2
            x = i * bar_width
            cr.rectangle(x, y, actual_bar_width, bar_h)
        cr.fill()

        cr.set_source_rgba(0.47, 0.47, 0.47, 0.4) # Grey
        for i in range(int(self.fraction * self.n_bars), self.n_bars):
            if i >= len(self.amplitudes): break
            bar_fraction = i / self.n_bars
            if bar_fraction <= self.fraction: continue
            
            amp = self.amplitudes[i]
            if amp > 0: amp = max(0.1, amp)
            bar_h = amp * height * 0.9
            y = (height - bar_h) / 2
            x = i * bar_width
            cr.rectangle(x, y, actual_bar_width, bar_h)
        cr.fill()

    def set_waveform_data(self, data):
        """Sets the raw waveform data (list of floats 0..1)."""
        # Normalize data to peak at 1.0
        if data:
            peak = max(data)
            if peak > 0:
                self.metric_data = [x / peak for x in data]
            else:
                 self.metric_data = data
        else:
            self.metric_data = []
        
        self.queue_draw()
        self._dirty_resample = True

    def _resample_data_to_bars(self):
        """Downsamples metric_data to len(self.amplitudes)."""
        if not self.metric_data:
            self.amplitudes = [0.0] * self.n_bars
            return

        total_samples = len(self.metric_data)
        
        new_amps = []
        for i in range(self.n_bars):
            # Calculate range in original data
            start_idx = int(i * total_samples / self.n_bars)
            end_idx = int((i + 1) * total_samples / self.n_bars)
            # Ensure at least one sample is picked if range is 0 (upsampling/nearest)
            if end_idx <= start_idx:
                end_idx = start_idx + 1
            
            # Clamp
            start_idx = min(start_idx, total_samples - 1)
            end_idx = min(end_idx, total_samples)
            if start_idx == end_idx: # Should rarely happen now
                 val = self.metric_data[start_idx] if start_idx < total_samples else 0.0
            else:
                 chunk = self.metric_data[start_idx:end_idx]
                 val = sum(chunk) / len(chunk)
            
            # Boost it a bit visually
            val = val * 1.5 
            if val > 1.0: val = 1.0
            
            new_amps.append(val)
        self.amplitudes = new_amps

    def set_fraction(self, fraction):
        self.fraction = max(0.0, min(1.0, fraction))
        self.queue_draw()

    def _on_pressed(self, gesture, n_press, x, y):
        width = self.get_width()
        if width > 0:
            fraction = x / width
            self.set_fraction(fraction)
            if self.seek_callback:
                self.seek_callback(fraction)




MPRIS_INTERFACE_XML = """
<node>
  <interface name='org.mpris.MediaPlayer2'>
    <method name='Raise'/>
    <method name='Quit'/>
    <property name='CanQuit' type='b' access='read'/>
    <property name='CanRaise' type='b' access='read'/>
    <property name='HasTrackList' type='b' access='read'/>
    <property name='Identity' type='s' access='read'/>
    <property name='DesktopEntry' type='s' access='read'/>
    <property name='SupportedUriSchemes' type='as' access='read'/>
    <property name='SupportedMimeTypes' type='as' access='read'/>
  </interface>
  <interface name='org.mpris.MediaPlayer2.Player'>
    <method name='Next'/>
    <method name='Previous'/>
    <method name='Pause'/>
    <method name='PlayPause'/>
    <method name='Stop'/>
    <method name='Play'/>
    <method name='Seek'>
      <arg direction='in' name='Offset' type='x'/>
    </method>
    <method name='SetPosition'>
      <arg direction='in' name='TrackId' type='o'/>
      <arg direction='in' name='Position' type='x'/>
    </method>
    <method name='OpenUri'>
      <arg direction='in' name='Uri' type='s'/>
    </method>
    <signal name='Seeked'>
      <arg name='Position' type='x'/>
    </signal>
    <property name='PlaybackStatus' type='s' access='read'/>
    <property name='LoopStatus' type='s' access='readwrite'/>
    <property name='Rate' type='d' access='readwrite'/>
    <property name='Shuffle' type='b' access='readwrite'/>
    <property name='Metadata' type='a{sv}' access='read'/>
    <property name='Volume' type='d' access='readwrite'/>
    <property name='Position' type='x' access='read'/>
    <property name='MinimumRate' type='d' access='read'/>
    <property name='MaximumRate' type='d' access='read'/>
    <property name='CanGoNext' type='b' access='read'/>
    <property name='CanGoPrevious' type='b' access='read'/>
    <property name='CanPlay' type='b' access='read'/>
    <property name='CanPause' type='b' access='read'/>
    <property name='CanSeek' type='b' access='read'/>
    <property name='CanControl' type='b' access='read'/>
  </interface>
</node>
"""

class MprisManager:
    def __init__(self, window):
        self.window = window
        self.bus_name = None
        self.registration_ids = []
        self.art_file = None
        
        self.node_info = Gio.DBusNodeInfo.new_for_xml(MPRIS_INTERFACE_XML)
        
        Gio.bus_get(Gio.BusType.SESSION, None, self._on_bus_acquired)

    def _on_bus_acquired(self, source, result):
        connection = Gio.bus_get_finish(result)
        if not connection:
            print("ERROR: Could not connect to D-Bus session bus.")
            return

        for interface in self.node_info.interfaces:
            reg_id = connection.register_object_with_closures2(
                "/org/mpris/MediaPlayer2",
                interface,
                self._handle_method_call,
                self._handle_get_property,
                self._handle_set_property
            )
            self.registration_ids.append(reg_id)

        self.bus_name = Gio.bus_own_name_on_connection(
            connection,
            "org.mpris.MediaPlayer2.mamo",
            Gio.BusNameOwnerFlags.NONE,
            None,
            None
        )
        self.connection = connection

    def update_playback_status(self):
        if not hasattr(self, 'connection'): return
        
        status = self._get_playback_status()
        self._emit_property_changed("org.mpris.MediaPlayer2.Player", {"PlaybackStatus": GLib.Variant("s", status)})

    def update_metadata(self, song):
        if not hasattr(self, 'connection'): return
        
        metadata = self._get_metadata_dict(song)
        self._emit_property_changed("org.mpris.MediaPlayer2.Player", {"Metadata": GLib.Variant("a{sv}", metadata)})

    def _emit_property_changed(self, interface_name, properties):
        if not hasattr(self, 'connection'): return
        
        # properties is a dict of {name: Variant}
        # PropertiesChanged signal: (s, a{sv}, as)
        self.connection.emit_signal(
            None,
            "/org/mpris/MediaPlayer2",
            "org.freedesktop.DBus.Properties",
            "PropertiesChanged",
            GLib.Variant("(sa{sv}as)", (interface_name, properties, []))
        )

    def _get_playback_status(self):
        if not self.window.player:
            return "Stopped"
        state = self.window.player.get_state(0).state
        if state == Gst.State.PLAYING:
            return "Playing"
        elif state == Gst.State.PAUSED:
            return "Paused"
        else:
            return "Stopped"

    def _get_metadata_dict(self, song):
        metadata = {}
        if not song:
            return metadata

        metadata["mpris:trackid"] = GLib.Variant("o", "/org/mpris/MediaPlayer2/Track/0")
        # song.duration is already in ns
        metadata["mpris:length"] = GLib.Variant("x", int(song.duration // 1000)) # NS to US
        metadata["xesam:title"] = GLib.Variant("s", song.title)
        metadata["xesam:artist"] = GLib.Variant("as", [song.artist])
        metadata["xesam:album"] = GLib.Variant("s", song.album)
        metadata["xesam:url"] = GLib.Variant("s", song.uri)

        # Handle Album Art
        if song.album_art_data:
            try:
                if self.art_file and os.path.exists(self.art_file):
                    os.unlink(self.art_file)
                
                with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as f:
                    f.write(song.album_art_data.get_data())
                    self.art_file = f.name
                
                metadata["mpris:artUrl"] = GLib.Variant("s", pathlib.Path(self.art_file).as_uri())
            except Exception as e:
                print(f"Error saving temp art for MPRIS: {e}")

        return metadata

    def _handle_method_call(self, connection, sender, object_path, interface_name, method_name, parameters, invocation):
        if interface_name == "org.mpris.MediaPlayer2":
            if method_name == "Raise":
                self.window.present()
                invocation.return_value(None)
            elif method_name == "Quit":
                self.window.get_application().quit()
                invocation.return_value(None)
        elif interface_name == "org.mpris.MediaPlayer2.Player":
            if method_name == "Next":
                GLib.idle_add(self.window._on_next_clicked)
                invocation.return_value(None)
            elif method_name == "Previous":
                GLib.idle_add(self.window._on_prev_clicked)
                invocation.return_value(None)
            elif method_name == "Pause":
                GLib.idle_add(self.window.player.set_state, Gst.State.PAUSED)
                invocation.return_value(None)
            elif method_name == "PlayPause":
                GLib.idle_add(self.window.toggle_play_pause)
                invocation.return_value(None)
            elif method_name == "Stop":
                GLib.idle_add(self.window.player.set_state, Gst.State.NULL)
                invocation.return_value(None)
            elif method_name == "Play":
                GLib.idle_add(self.window.player.set_state, Gst.State.PLAYING)
                invocation.return_value(None)

    def _handle_get_property(self, connection, sender, object_path, interface_name, property_name):
        if interface_name == "org.mpris.MediaPlayer2":
            if property_name == "CanQuit": return GLib.Variant("b", True)
            if property_name == "CanRaise": return GLib.Variant("b", True)
            if property_name == "HasTrackList": return GLib.Variant("b", False)
            if property_name == "Identity": return GLib.Variant("s", "Mamo")
            if property_name == "DesktopEntry": return GLib.Variant("s", "mamo")
            if property_name == "SupportedUriSchemes": return GLib.Variant("as", ["file"])
            if property_name == "SupportedMimeTypes": return GLib.Variant("as", ["audio/mpeg", "audio/flac", "audio/ogg"])
        elif interface_name == "org.mpris.MediaPlayer2.Player":
            if property_name == "PlaybackStatus": return GLib.Variant("s", self._get_playback_status())
            if property_name == "Metadata": return GLib.Variant("a{sv}", self._get_metadata_dict(self.window.current_song))
            if property_name == "CanGoNext": return GLib.Variant("b", True)
            if property_name == "CanGoPrevious": return GLib.Variant("b", True)
            if property_name == "CanPlay": return GLib.Variant("b", True)
            if property_name == "CanPause": return GLib.Variant("b", True)
            if property_name == "CanControl": return GLib.Variant("b", True)
            if property_name == "CanSeek": return GLib.Variant("b", False)
            if property_name == "Position": return GLib.Variant("x", 0)
            if property_name == "Volume": return GLib.Variant("d", 1.0)
        return None

    def _handle_set_property(self, connection, sender, object_path, interface_name, property_name, value):
        return False


class MamoWindow(Adw.ApplicationWindow):
    PLAY_ICON = "media-playback-start-symbolic"
    PAUSE_ICON = "media-playback-pause-symbolic"
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.set_title("Mamo")
        self.set_default_size(500, 600)
        self.current_song = None
        self._last_indicated_song = None
        self._auto_play_after_load = False
        self._save_timer_id = None
        self._progress_timer_id = None
        self._waveform_push_ctr = 0
        self._is_switching = False
        self._playlist_file_path = os.path.expanduser("~/.config/mamo/playlist.json")
        self._settings_file_path = os.path.expanduser("~/.config/mamo/settings.json")
        self.duration_ns = 0 
        self._waveform_cache_dir = os.path.expanduser("~/.cache/mamo/waveforms")
        os.makedirs(self._waveform_cache_dir, exist_ok=True)
        
        self._init_player()
        self._setup_actions()
        self._auto_play_after_load = False
        self._external_art_cache = {} # dirpath -> GLib.Bytes
        self._active_analysis_uris = set()
        self._analysis_queue = collections.deque()
        self._analysis_worker_running = False
        self._is_loading = False
        self.mpris = None
        self.library_manager = None
        
        self.library_path = os.path.expanduser("~/Music")
        self._load_settings()

        self._library_cache_path = os.path.expanduser("~/.cache/mamo/library.json")
        
        def deferred_init():
            # Deferred MPRIS
            self.mpris = MprisManager(self)
            
            # Deferred Library Manager
            self.library_manager = LibraryManager(self.library_path, self._library_cache_path)
            return False

        GLib.idle_add(deferred_init)

        # Toast Overlay

        
        # Toast Overlay
        self.toast_overlay = Adw.ToastOverlay()
        self.set_content(self.toast_overlay)

        toolbar_view = Adw.ToolbarView()
        self.toast_overlay.set_child(toolbar_view)

        header = Adw.HeaderBar.new()
        toolbar_view.add_top_bar(header) 

        # Main content stack (Status Page vs Playlist)
        self.main_stack = Gtk.Stack()
        self.main_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        toolbar_view.set_content(self.main_stack)

        # 1. Empty State Page
        status_page = Adw.StatusPage()
        status_page.set_icon_name("folder-music-symbolic")
        status_page.set_title("No Music")
        status_page.set_description("Add a folder or open a playlist to start listening.")
        
        status_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        status_box.set_halign(Gtk.Align.CENTER)
        
        button_container = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        button_container.set_halign(Gtk.Align.CENTER)
        status_box.append(button_container)

        add_file_btn = Gtk.Button(label="Add File")
        add_file_btn.add_css_class("pill")
        add_file_btn.connect("clicked", self._on_add_file_clicked)
        button_container.append(add_file_btn)

        add_folder_pill = Gtk.Button(label="Add Folder")
        add_folder_pill.add_css_class("pill")
        add_folder_pill.connect("clicked", self._on_add_clicked)
        button_container.append(add_folder_pill)
        
        browse_music_btn = Gtk.Button(label="Browse Music")
        browse_music_btn.add_css_class("pill")
        browse_music_btn.add_css_class("suggested-action")
        browse_music_btn.connect("clicked", self._on_play_album_clicked)
        button_container.append(browse_music_btn)
        
        status_page.set_child(status_box)
        self.main_stack.add_named(status_page, "empty")

        # 3. Loading Page
        loading_page = Adw.StatusPage()
        loading_page.set_icon_name("folder-music-symbolic")
        loading_page.set_title("Loading...")
        loading_page.set_description("Please wait while we load your music.")
        
        loading_spinner = Gtk.Spinner()
        loading_spinner.start()
        loading_spinner.set_halign(Gtk.Align.CENTER)
        
        loading_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        loading_box.set_halign(Gtk.Align.CENTER)
        loading_box.append(loading_spinner)
        
        loading_page.set_child(loading_box)
        self.main_stack.add_named(loading_page, "loading")

        # 2. Main Player View works (Box)
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.main_stack.add_named(main_box, "player")

        
        playback_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        playback_box.add_css_class("linked") 

        self.prev_button = Gtk.Button.new_from_icon_name("media-skip-backward-symbolic")
        self.prev_button.connect("clicked", self._on_prev_clicked)
        playback_box.append(self.prev_button)

        self.play_pause_button = Gtk.Button.new_from_icon_name("media-playback-start-symbolic")
        self.play_pause_button.connect("clicked", self.toggle_play_pause)
        playback_box.append(self.play_pause_button)

        self.next_button = Gtk.Button.new_from_icon_name("media-skip-forward-symbolic")
        self.next_button.connect("clicked", self._on_next_clicked)
        playback_box.append(self.next_button)

        header.pack_start(playback_box)

        
        main_menu = Gio.Menu()
        
        main_menu.append("Open Playlist", "win.open_playlist")
        main_menu.append("Save Playlist", "win.save_playlist")
        main_menu.append("Add Folder...", "win.add_folder_new") 
        main_menu.append("Add Files...", "win.add_file")
        main_menu.append("Clear Playlist", "win.clear_playlist")
        
        section = Gio.Menu()
        section.append("Use Dark Mode", "win.dark_mode")
        section.append("Auto Play", "win.auto_play")
        section.append("Loop All", "win.loop_all")
        section.append("Clear Playlist on Start", "win.clear_on_start")
        section.append("About", "win.about")
        main_menu.append_section(None, section)

        menu_button = Gtk.MenuButton.new()
        menu_button.set_icon_name("open-menu-symbolic")
        menu_button.set_menu_model(main_menu) 

        play_album_button = Gtk.Button.new_from_icon_name("folder-music-symbolic")
        play_album_button.connect("clicked", self._on_play_album_clicked)
        play_album_button.set_tooltip_text("Album Browser")

        header.pack_end(menu_button) 
        header.pack_end(play_album_button)

        song_info_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        song_info_box.set_margin_start(12)
        song_info_box.set_margin_end(12)
        song_info_box.set_margin_top(6)
        song_info_box.set_margin_bottom(6)
        main_box.append(song_info_box)

        self.cover_image = Gtk.Image.new_from_icon_name("audio-x-generic-symbolic") 
        self.cover_image.set_pixel_size(100)
        self.cover_image.add_css_class("album-art-image") 
        self.cover_image.set_visible(True)
        song_info_box.append(self.cover_image)

        song_details_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        song_details_box.set_valign(Gtk.Align.CENTER)
        song_info_box.append(song_details_box)

        self.song_label = Gtk.Label(label="", xalign=0)
        self.song_label.set_ellipsize(Pango.EllipsizeMode.END) 
        self.song_label.add_css_class("title-5") 
        song_details_box.append(self.song_label)

        self.artist_label = Gtk.Label(label="", xalign=0)
        self.artist_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.artist_label.add_css_class("caption")
        song_details_box.append(self.artist_label)

        # Waveform Progress Section
        progress_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        progress_box.set_halign(Gtk.Align.FILL)
        song_details_box.append(progress_box)

        # Current Time Label
        self.time_label_current = Gtk.Label(label="0:00", xalign=1)
        self.time_label_current.add_css_class("caption")
        self.time_label_current.set_width_chars(5)
        progress_box.append(self.time_label_current)

        # Waveform Widget
        self.waveform = WaveformBar(seek_callback=self._on_waveform_seek)
        self.waveform.set_hexpand(True)
        progress_box.append(self.waveform)

        # Remaining Time Label (Negative)
        self.time_label_remaining = Gtk.Label(label="-0:00", xalign=0)
        self.time_label_remaining.add_css_class("caption")
        self.time_label_remaining.set_width_chars(6)
        progress_box.append(self.time_label_remaining)

        # Add DropTarget to status_box for external files
        empty_drop_target = Gtk.DropTarget.new(Gdk.FileList.__gtype__, Gdk.DragAction.COPY)
        empty_drop_target.connect("drop", self._on_external_drop)
        status_page.add_controller(empty_drop_target)

        main_box.append(Gtk.Label(label="")) # Spacer

        


        

        scrolled_window = Gtk.ScrolledWindow()
        scrolled_window.set_vexpand(True)
        scrolled_window.set_hexpand(False)
        scrolled_window.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        main_box.append(scrolled_window)

        
        self.playlist_store = Gio.ListStore(item_type=Song) 

        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._on_playlist_item_setup)
        factory.connect("bind", self._on_playlist_item_bind)
        factory.connect("unbind", self._on_playlist_item_unbind)

        self.selection_model = Gtk.SingleSelection(model=self.playlist_store)
        self.selection_model.connect("selection-changed", self._on_playlist_selection_changed)
        self.selection_model.connect("selection-changed", lambda *a: self._update_playback_controls_sensitivity())
        # Removed remaining time update signal

        self.playlist_view = Gtk.ListView(model=self.selection_model,
                                          factory=factory)
        self.playlist_view.set_vexpand(True)
        self.playlist_view.set_vexpand(False)
        self.playlist_view.add_css_class("playlist-view") # For zebra striping
        scrolled_window.set_child(self.playlist_view)

        # Add DropTarget to playlist_view for external files
        playlist_drop_target = Gtk.DropTarget.new(Gdk.FileList.__gtype__, Gdk.DragAction.COPY)
        playlist_drop_target.connect("drop", self._on_external_drop)
        self.playlist_view.add_controller(playlist_drop_target)

        
        key_controller = Gtk.EventControllerKey()
        key_controller.connect("key-pressed", self._on_playlist_key_pressed)
        self.playlist_view.add_controller(key_controller)

        
        
        self.playlist_store.connect("items-changed", self._update_viewport) 
        self.playlist_store.connect("items-changed", lambda *a: self._update_playback_controls_sensitivity())
        
        clear_on_start = self.action_group.get_action_state("clear_on_start").get_boolean()
        if not clear_on_start:
            self._load_playlist()
        else:
            print("Clear Playlist on Start is enabled. Starting empty.")

        self._update_viewport()
        self._update_song_display(None)
        # Removed initial remaining time update call

    def _update_viewport(self, model=None, position=None, removed=None, added=None):
        """Switches between empty state and player view based on playlist content."""
        if self._is_loading:
            self.main_stack.set_visible_child_name("loading")
            self.play_pause_button.set_sensitive(False)
        elif self.playlist_store.get_n_items() > 0:
            self.main_stack.set_visible_child_name("player")
            self.play_pause_button.set_sensitive(True) 
            self._update_playback_controls_sensitivity()
        else:
            self.main_stack.set_visible_child_name("empty")
            self.play_pause_button.set_sensitive(False) 
            self._update_playback_controls_sensitivity()

    def _update_playback_controls_sensitivity(self):
        """Updates the sensitivity of Previous and Next buttons."""
        n_items = self.playlist_store.get_n_items()
        if n_items == 0:
            self.prev_button.set_sensitive(False)
            self.next_button.set_sensitive(False)
            return

        loop_all = self.action_group.get_action_state("loop_all").get_boolean()
        current_pos = self.selection_model.get_selected()

        if loop_all:
            self.prev_button.set_sensitive(True)
            self.next_button.set_sensitive(True)
        else:
            # Not looping: disable buttons at boundaries
            has_pos = current_pos != Gtk.INVALID_LIST_POSITION
            self.prev_button.set_sensitive(has_pos and current_pos > 0)
            self.next_button.set_sensitive(current_pos < (n_items - 1) or not has_pos)

    
    def _setup_actions(self):
        action_group = Gio.SimpleActionGroup()

        open_action = Gio.SimpleAction.new("open_playlist", None)
        open_action.connect("activate", self._on_open_playlist_action)
        action_group.add_action(open_action)

        save_action = Gio.SimpleAction.new("save_playlist", None)
        save_action.connect("activate", self._on_save_playlist_action)
        action_group.add_action(save_action)

        add_folder_action = Gio.SimpleAction.new("add_folder_new", None) 
        add_folder_action.connect("activate", self._on_add_folder_action)
        action_group.add_action(add_folder_action)

        add_file_action = Gio.SimpleAction.new("add_file", None)
        add_file_action.connect("activate", self._on_add_file_action)
        action_group.add_action(add_file_action)

        clear_action = Gio.SimpleAction.new("clear_playlist", None)
        clear_action.connect("activate", self._on_clear_playlist_action)
        action_group.add_action(clear_action)

        remove_action = Gio.SimpleAction.new("remove_selected_song", None)
        remove_action.connect("activate", self._on_remove_selected_song_action)
        action_group.add_action(remove_action)

        # Stateful dark mode action
        dark_mode_action = Gio.SimpleAction.new_stateful("dark_mode", None, GLib.Variant.new_boolean(False))
        dark_mode_action.connect("activate", self._on_dark_mode_action_activated)
        action_group.add_action(dark_mode_action)

        # Stateful auto play action - default to True
        auto_play_action = Gio.SimpleAction.new_stateful("auto_play", None, GLib.Variant.new_boolean(True))
        auto_play_action.connect("activate", self._on_auto_play_action_activated)
        action_group.add_action(auto_play_action)

        # Stateful clear on start action
        clear_on_start_action = Gio.SimpleAction.new_stateful("clear_on_start", None, GLib.Variant.new_boolean(False))
        clear_on_start_action.connect("activate", self._on_clear_on_start_action_activated)
        action_group.add_action(clear_on_start_action)

        about_action = Gio.SimpleAction.new("about", None)
        about_action.connect("activate", self._on_about_action)
        action_group.add_action(about_action)

        # Stateful repeat action
        repeat_action = Gio.SimpleAction.new_stateful("repeat", None, GLib.Variant.new_boolean(False))
        repeat_action.connect("activate", self._on_repeat_action_activated)
        action_group.add_action(repeat_action)

        # Stateful loop all action
        loop_all_action = Gio.SimpleAction.new_stateful("loop_all", None, GLib.Variant.new_boolean(False))
        loop_all_action.connect("activate", self._on_loop_all_action_activated)
        action_group.add_action(loop_all_action)

        self.action_group = action_group
        self.insert_action_group("win", action_group)

    def _init_player(self):
        """Initialize GStreamer player and discoverer."""
        
        self.discoverer = GstPbutils.Discoverer.new(5 * Gst.SECOND) 
        self.discoverer.connect("discovered", self._on_discoverer_discovered)
        self.discoverer.connect("finished", self._on_discoverer_finished)
        self.discoverer.start() 

        
        self.player = Gst.ElementFactory.make("playbin", "player")
        if not self.player:
            print("ERROR: Could not create GStreamer playbin element.", file=sys.stderr)
            
            return
        
        # Build audio-filter bin: audioconvert → audioresample → rgvolume → level → audioconvert
        afilter = Gst.Bin.new("afilter")

        aconv1 = Gst.ElementFactory.make("audioconvert", None)
        ares   = Gst.ElementFactory.make("audioresample", None)
        rgvol  = Gst.ElementFactory.make("rgvolume", None)
        level  = Gst.ElementFactory.make("level", None)
        aconv2 = Gst.ElementFactory.make("audioconvert", None)

        if not all([afilter, aconv1, ares, rgvol, level, aconv2]):
            print("ERROR: Could not create audio filter elements.", file=sys.stderr)
            return

        level.set_property("post-messages", True)
        level.set_property("interval", 250 * 1000000)  # 250ms

        afilter.add(aconv1)
        afilter.add(ares)
        afilter.add(rgvol)
        afilter.add(level)
        afilter.add(aconv2)

        aconv1.link(ares)
        ares.link(rgvol)
        rgvol.link(level)
        level.link(aconv2)

        afilter.add_pad(Gst.GhostPad.new("sink", aconv1.get_static_pad("sink")))
        afilter.add_pad(Gst.GhostPad.new("src", aconv2.get_static_pad("src")))

        self._level_elem = level
        self.player.set_property("audio-filter", afilter)

        
        bus = self.player.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_player_message)

    def play_uri(self, uri):
        """Loads and starts playing a URI."""
        if self._is_switching:
            print(f"play_uri: already switching, ignoring request for {uri}")
            return
            
        self._is_switching = True
        try:
            if not self.player:
                self.current_song = None
                return
    
            
            self.current_song = None
            n = self.playlist_store.get_n_items()
            for i in range(n):
                 song = self.playlist_store.get_item(i)
                 if song.uri == uri:
                     self.current_song = song
                     break
    
            print(f"play_uri: uri={uri} target_found={self.current_song is not None}")
            if self.current_song:
                 print(f"play_uri: song_title='{self.current_song.title}'")
    
            self._update_song_display(self.current_song)
            self.mpris.update_metadata(self.current_song)
            self.mpris.update_playback_status()
            
            print(f"Playing URI: {uri}")
            self.player.set_property("uri", uri)
            self.player.set_state(Gst.State.PLAYING)
            
            self.play_pause_button.set_icon_name(self.PAUSE_ICON)
            
            self.play_pause_button.set_icon_name(self.PAUSE_ICON)
            
            self.duration_ns = 0
            self._auto_play_after_load = False
        finally:
            self._is_switching = False 
        

    def toggle_play_pause(self, button=None):
        """Toggles playback state."""
        if not self.player: return
        self.mpris.update_playback_status()

        state = self.player.get_state(0).state
        if state == Gst.State.PLAYING:
            print("Pausing playback")
            self.player.set_state(Gst.State.PAUSED)
            self.play_pause_button.set_icon_name(self.PLAY_ICON)
        elif state == Gst.State.PAUSED or state == Gst.State.READY:
             
            print("Resuming/Starting playback")
            self.player.set_state(Gst.State.PLAYING)
            self.play_pause_button.set_icon_name(self.PAUSE_ICON)
        elif state == Gst.State.NULL:
            
            print("No media loaded to play.")
            
            pass
        


    def _on_playlist_item_setup(self, factory, list_item):
        """Setup widgets for a song row."""
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        box.set_margin_start(6)
        box.set_margin_end(6)
        box.set_margin_top(6)
        box.set_margin_bottom(6)

        # "Now Playing" indicator icon wrapped in a fixed-width box to prevent shifting
        indicator_container = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        indicator_container.set_size_request(24, -1)
        indicator_container.set_valign(Gtk.Align.CENTER)
        indicator_container.set_halign(Gtk.Align.CENTER)

        status_icon = Gtk.Image.new_from_icon_name("media-playback-start-symbolic")
        status_icon.set_visible(False)
        list_item._status_icon = status_icon
        status_icon.add_css_class("now-playing-indicator")
        indicator_container.append(status_icon)
        box.append(indicator_container)

        # Container for vertical labels
        label_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        label_box.set_valign(Gtk.Align.CENTER)
        label_box.set_hexpand(True)

        title_label = Gtk.Label(xalign=0)
        title_label.set_ellipsize(Pango.EllipsizeMode.END) 
        title_label.add_css_class("playlist-song-title")
        title_label.set_max_width_chars(60)

        subtitle_label = Gtk.Label(xalign=0)
        subtitle_label.set_ellipsize(Pango.EllipsizeMode.END)
        subtitle_label.add_css_class("playlist-song-subtitle")
        subtitle_label.set_max_width_chars(60)

        label_box.append(title_label)
        label_box.append(subtitle_label)
        box.append(label_box)
        list_item.set_child(box)

        
        gesture = Gtk.GestureClick.new()
        box.add_controller(gesture)
        
        list_item._click_gesture = gesture

        gesture_right = Gtk.GestureClick.new()
        gesture_right.set_button(3) # Right click
        box.add_controller(gesture_right)
        list_item._right_click_gesture = gesture_right

        # Drag Source for reordering
        drag_source = Gtk.DragSource.new()
        drag_source.set_actions(Gdk.DragAction.MOVE)
        drag_source.connect("prepare", self._on_row_drag_prepare, list_item)
        box.add_controller(drag_source)

        # Drop Target for reordering
        drop_target = Gtk.DropTarget.new(Song.__gtype__, Gdk.DragAction.MOVE)
        drop_target.connect("drop", self._on_row_drop, list_item)
        box.add_controller(drop_target)



    def _on_playlist_item_bind(self, factory, list_item):
        """Bind song data to the widgets."""
        box = list_item.get_child()
        indicator_container = box.get_first_child()
        status_icon = indicator_container.get_first_child()
        label_box = box.get_last_child()
        title_label = label_box.get_first_child()
        subtitle_label = label_box.get_last_child()

        song = list_item.get_item() 

        # Bind is_playing to status_icon visibility
        # Use the stored status_icon and handle previous bindings to prevent leaks in recycled rows
        status_icon = getattr(list_item, "_status_icon", None)
        if not status_icon:
            indicator_container = box.get_first_child()
            status_icon = indicator_container.get_first_child()

        if hasattr(list_item, "_playing_binding") and list_item._playing_binding:
            list_item._playing_binding.unbind()
            
        list_item._playing_binding = song.bind_property("is_playing", status_icon, "visible", GObject.BindingFlags.SYNC_CREATE)

        title_label.set_label(song.title)
        
        # Subtitle: Artist - Album (if available)
        subtitle_parts = []
        if song.artist and song.artist != "Unknown Artist":
            subtitle_parts.append(song.artist)
        if song.album and song.album != "Unknown Album":
            subtitle_parts.append(song.album)
        
        subtitle_text = " - ".join(subtitle_parts) if subtitle_parts else "Unknown Artist"
        subtitle_label.set_label(subtitle_text)

        full_tooltip = f"{song.title}\n{subtitle_text}"
        title_label.set_tooltip_text(full_tooltip)
        subtitle_label.set_tooltip_text(full_tooltip)

        # Bind left click
        gesture = getattr(list_item, "_click_gesture", None)
        if gesture and isinstance(gesture, Gtk.GestureClick):
            handler_id = getattr(list_item, "_click_handler_id", None)
            if handler_id:
                try:
                    gesture.disconnect(handler_id)
                except TypeError: pass
            new_handler_id = gesture.connect("pressed", self._on_song_row_activated, song)
            list_item._click_handler_id = new_handler_id

        # Bind right click
        gesture_right = getattr(list_item, "_right_click_gesture", None)
        if gesture_right and isinstance(gesture_right, Gtk.GestureClick):
            handler_id_right = getattr(list_item, "_right_click_handler_id", None)
            if handler_id_right:
                try:
                    gesture_right.disconnect(handler_id_right)
                except TypeError: pass
            new_handler_id_right = gesture_right.connect("pressed", self._on_song_row_right_clicked, song, list_item)
            list_item._right_click_handler_id = new_handler_id_right

    def _on_playlist_item_unbind(self, factory, list_item):
        """Unbind song data from the widgets."""
        if hasattr(list_item, "_playing_binding") and list_item._playing_binding:
            list_item._playing_binding.unbind()
            list_item._playing_binding = None

    def _on_row_drag_prepare(self, source, x, y, list_item):
        """Prepares the drag operation for a playlist row."""
        song = list_item.get_item()
        if not song:
            return None
        
        # Use a ContentProvider for the Song object
        value = GObject.Value(Song, song)
        return Gdk.ContentProvider.new_for_value(value)

    def _on_row_drop(self, target, song, x, y, list_item):
        """Handles dropping a Song onto another row for reordering."""
        target_pos = list_item.get_position()
        if target_pos == Gtk.INVALID_LIST_POSITION:
            return False

        # Find the source position
        source_pos = -1
        for i in range(self.playlist_store.get_n_items()):
            if self.playlist_store.get_item(i) == song:
                source_pos = i
                break
        
        if source_pos == -1 or source_pos == target_pos:
            return False

        print(f"Moving song from {source_pos} to {target_pos}")
        
        # Move in the store
        # Optimization: remove and insert
        # We need to be careful with selection if we want to keep it
        is_selected = self.selection_model.get_selected() == source_pos
        
        self.playlist_store.remove(source_pos)
        self.playlist_store.insert(target_pos, song)
        
        if is_selected:
            self.selection_model.set_selected(target_pos)
            
        return True

    def _on_external_drop(self, target, value, x, y):
        """Handles dropping files/folders from an external application."""
        if not isinstance(value, Gdk.FileList):
            return False
        
        files = value.get_files()
        print(f"External drop: {len(files)} items")
        
        for gio_file in files:
            uri = gio_file.get_uri()
            path = gio_file.get_path()
            
            if not path:
                # Handle non-local URIs if needed, but discoverer likes URIs
                self._discover_and_add_uri(uri)
                continue

            if os.path.isdir(path):
                print(f"External drop: Adding directory {path}")
                self._start_folder_scan(gio_file)
            else:
                print(f"External drop: Adding file {uri}")
                self._discover_and_add_uri(uri)
        
        return True

    def _on_song_row_right_clicked(self, gesture, n_press, x, y, song, list_item):
        """Shows a context menu on right click."""
        if n_press == 1:
            # First, select this item
            position = list_item.get_position()
            if position != Gtk.INVALID_LIST_POSITION:
                self.selection_model.set_selected(position)

            menu = Gio.Menu()
            menu.append("Remove from Playlist", "win.remove_selected_song")
            menu.append("Repeat Song", "win.repeat")
            
            popover = Gtk.PopoverMenu.new_from_model(menu)
            popover.set_parent(list_item.get_child())
            
            # Point to the click location
            rect = Gdk.Rectangle()
            rect.x = x
            rect.y = y
            rect.width = 1
            rect.height = 1
            popover.set_pointing_to(rect)
            
            popover.popup()

    def _on_remove_selected_song_action(self, action, param):
        """Action handler to remove the selected song."""
        position = self.selection_model.get_selected()
        if position != Gtk.INVALID_LIST_POSITION:
            song = self.playlist_store.get_item(position)
            if song == self.current_song:
                print("Removing currently playing song, stopping playback.")
                if self.player:
                    self.player.set_state(Gst.State.NULL)
                self.current_song = None
                self._update_song_display(None)
            
            print(f"Removing song at position: {position}")
            self.playlist_store.remove(position)

    def _on_clear_playlist_action(self, action, param):
        """Action handler to clear the entire playlist."""
        print("Clearing playlist.")
        if self.player:
            self.player.set_state(Gst.State.NULL)
            self._update_song_display(None)
        self.playlist_store.remove_all()

    def _on_song_row_activated(self, gesture, n_press, x, y, song):
        """Handles activation (double-click) on a playlist row."""
        
        if n_press == 2:
            print(f"Double-clicked/Activated song: {song.title}")
            if song and song.uri:
                 
                 if self.player:
                     print("Stopping current playback due to activation.")
                     self.player.set_state(Gst.State.NULL)
                 
                 self.play_uri(song.uri)
            else:
                 print("Cannot play activated item (no URI?).")


    def _on_playlist_selection_changed(self, selection_model, position, n_items):
        """Callback when the selected song in the playlist changes."""
        # We no longer update the 'Now Playing' display on simple selection,
        # as it should reflect the currently playing song only.
        selected_item = selection_model.get_selected_item()
        if selected_item:
            print(f"Selected: {selected_item.artist} - {selected_item.title}")

    def _on_playlist_key_pressed(self, controller, keyval, keycode, state):
        """Handles key presses on the playlist view, specifically Delete/Backspace."""
        if keyval == Gdk.KEY_Delete or keyval == Gdk.KEY_BackSpace:
            position = self.selection_model.get_selected()
            if position != Gtk.INVALID_LIST_POSITION:
                song = self.playlist_store.get_item(position)
                if song == self.current_song:
                    print("Deleting currently playing song, stopping playback.")
                    if self.player:
                        self.player.set_state(Gst.State.NULL)
                    self.current_song = None
                    self._update_song_display(None)

                print(f"Deleting item at position: {position}")
                self.playlist_store.remove(position)
                
                return True 
        return False 

    def _on_add_clicked(self, button):
        """Handles the Add Folder pill click."""
        self._auto_play_after_load = False
        self._on_add_folder_action(None, None)

    def _on_add_file_clicked(self, button):
        """Handles the Add File button click."""
        self._auto_play_after_load = False
        self._show_file_dialog()

    def _on_play_album_clicked(self, button):
        """Handles the Play Album button click: opens the Album Browser."""
        if not self.library_manager:
            print("LibraryManager not yet initialized.")
            return

        print("Play Album clicked. Opening Browser.")
        browser = AlbumBrowser(self, self.library_manager, self._on_album_browser_callback)
        browser.present()

    def _on_album_browser_callback(self, album, action):
        """Callback from AlbumBrowser."""
        print(f"AlbumBrowser callback: {album.title} by {album.artist}, Action: {action}")
        
        if action == "play":
            # Don't remove_all() anymore, just stop current playback so the new album can start
            if self.player:
                self.player.set_state(Gst.State.NULL)
            self.current_song = None
            self._update_song_display(None)

        # Add all audio files from folder
        folder = album.folder
        if os.path.isdir(folder):
            for fn in sorted(os.listdir(folder)):
                if fn.lower().endswith(('.mp3', '.flac', '.m4a', '.ogg')):
                    uri = pathlib.Path(os.path.join(folder, fn)).as_uri()
                    self._discover_and_add_uri(uri)
            
            if action == "play":
                # Wait a bit for discovery or just rely on auto_play logic if we had it
                # For now, let's set a flag to play the first item added
                self._auto_play_after_load = True

    def _show_file_dialog(self):
        """Internal helper to show the file dialog."""
        dialog = Gtk.FileDialog.new()
        dialog.set_title("Add Music Files")
        dialog.set_modal(True)
        dialog.open_multiple(parent=self, cancellable=None,
                             callback=self._on_file_dialog_open_multiple_finish)

    def _on_file_dialog_open_multiple_finish(self, dialog, result):
        """Handles the response from the Gtk.FileDialog."""
        try:
            files = dialog.open_multiple_finish(result)
            if files:
                print(f"Processing {files.get_n_items()} selected items...")
                for i in range(files.get_n_items()):
                    gio_file = files.get_item(i) 
                    if not gio_file: continue

                    try:
                        
                        info = gio_file.query_info(
                            Gio.FILE_ATTRIBUTE_STANDARD_TYPE,
                            Gio.FileQueryInfoFlags.NONE,
                            None
                        )
                        file_type = info.get_file_type()

                        if file_type == Gio.FileType.REGULAR:
                            print(f"Adding regular file: {gio_file.get_uri()}")
                            self._discover_and_add_uri(gio_file.get_uri()) 
                        elif file_type == Gio.FileType.DIRECTORY:
                            print(f"Starting scan for directory: {gio_file.get_path()}")
                            self._start_folder_scan(gio_file) 
                        else:
                            print(f"Skipping unsupported file type: {gio_file.get_path()}")

                    except GLib.Error as info_err:
                         print(f"Error querying info for {gio_file.peek_path()}: {info_err.message}", file=sys.stderr)
                    except Exception as proc_err: 
                         print(f"Error processing item {gio_file.peek_path()}: {proc_err}", file=sys.stderr)

        except GLib.Error as e:
            
            if e.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED):
                print("File selection cancelled.")
            else:
                
                print(f"Error opening files: {e.message}", file=sys.stderr)
        except Exception as general_e: 
             print(f"Unexpected error during file dialog finish: {general_e}", file=sys.stderr)

    def _start_folder_scan(self, folder_gio_file):
        """Starts a background thread to scan a folder for audio files."""
        
        folder_path = folder_gio_file.get_path()
        if folder_path and os.path.isdir(folder_path):
            print(f"Starting background scan thread for: {folder_path}")
            thread = threading.Thread(target=self._scan_folder_thread, args=(folder_path,), daemon=True)
            thread.start()
        else:
            print(f"Cannot scan folder: Invalid path or not a directory ({folder_path})", file=sys.stderr)

    def _scan_folder_thread(self, folder_path):
        """Background thread function to recursively scan a folder for audio files."""
        print(f"Thread '{threading.current_thread().name}': Scanning {folder_path}")
        audio_extensions = {".mp3", ".flac", ".ogg", ".opus", ".m4a", ".wav", ".aac"}
        files_found = 0
        files_added = 0

        try:
            for root, _, filenames in os.walk(folder_path):
                for filename in filenames:
                    if filename.lower().endswith(tuple(audio_extensions)):
                        files_found += 1
                        full_path = os.path.join(root, filename)
                        try:
                            file_uri = pathlib.Path(full_path).as_uri()
                            
                            song_object = self._discover_uri_sync(file_uri, full_path)

                            if song_object:
                                files_added += 1
                                
                                def append_and_check(s=song_object):
                                    self.playlist_store.append(s)
                                    if self._auto_play_after_load:
                                        self._check_and_autoplay(specific_song=s)

                                GLib.idle_add(append_and_check)
                                
                        except Exception as file_proc_err:
                            print(f"Thread '{threading.current_thread().name}': Error processing file {full_path}: {file_proc_err}", file=sys.stderr)

        except Exception as walk_err:
            print(f"Thread '{threading.current_thread().name}': Error walking directory {folder_path}: {walk_err}", file=sys.stderr)

        print(f"Thread '{threading.current_thread().name}': Finished scanning {folder_path}. Found: {files_found}, Added: {files_added}")


    def _find_external_art(self, filepath):
        """
        Looks for external image files in the same directory as the audio file.
        Returns GLib.Bytes if an image is found, otherwise None.
        """
        if not filepath or not os.path.exists(filepath):
            return None

        directory = os.path.dirname(filepath)
        if not os.path.isdir(directory):
            return None

        # 1. Check Cache
        if directory in self._external_art_cache:
            return self._external_art_cache[directory]

        # 2. Case-insensitive check on disk
        # Common bases for album art 
        common_bases = ["cover", "folder", "album", "front", "art"]
        extensions = [".jpg", ".jpeg", ".png", ".webp"]

        try:
            files = os.listdir(directory)
            # 2.1 Look for common names first
            for base in common_bases:
                for filename in files:
                    name, ext = os.path.splitext(filename)
                    if name.lower() == base and ext.lower() in extensions:
                        image_path = os.path.join(directory, filename)
                        try:
                            with open(image_path, "rb") as f:
                                art_bytes = GLib.Bytes.new(f.read())
                                self._external_art_cache[directory] = art_bytes
                                return art_bytes
                        except Exception as e:
                            print(f"Error reading external art {image_path}: {e}", file=sys.stderr)

            # 2.2 Fallback to any image file in the directory
            for filename in files:
                if any(filename.lower().endswith(ext) for ext in extensions):
                    image_path = os.path.join(directory, filename)
                    try:
                        with open(image_path, "rb") as f:
                            art_bytes = GLib.Bytes.new(f.read())
                            self._external_art_cache[directory] = art_bytes
                            return art_bytes
                    except Exception:
                        continue
        except Exception as e:
            print(f"Error listing directory {directory} for art: {e}", file=sys.stderr)

        self._external_art_cache[directory] = None
        return None


    def _discover_uri_sync(self, uri, filepath):
        """
        Synchronous helper to discover metadata and art for a single file path.
        Runs within the background folder scanning thread.
        Uses Mutagen primarily. Returns a Song object or None.
        """
        
        mutagen_title = None
        mutagen_artist = None
        mutagen_album = None
        album_art_bytes = None
        album_art_glib_bytes = None
        duration_ns = 0 

        try:
            if not os.path.exists(filepath):
                 print(f"Sync Discover Error: File path does not exist: {filepath}", file=sys.stderr)
                 return None

            
            try:
                
                audio_easy = mutagen.File(filepath, easy=True)
                if audio_easy:
                    mutagen_title = audio_easy.get('title', [None])[0]
                    mutagen_artist = audio_easy.get('artist', [None])[0]
                    mutagen_album = audio_easy.get('album', [None])[0]
                    
                    duration_str = audio_easy.get('length', [None])[0]
                    if duration_str:
                        try: duration_ns = int(float(duration_str) * Gst.SECOND)
                        except (ValueError, TypeError): pass 
            except Exception as tag_e:
                print(f"Sync Discover: Mutagen error reading easy tags from {filepath}: {tag_e}", file=sys.stderr)

            # --- Embedded Art Priority ---
            try:
                audio_raw = mutagen.File(filepath)
                if audio_raw:
                    if duration_ns <= 0 and audio_raw.info and hasattr(audio_raw.info, 'length'):
                        try: duration_ns = int(audio_raw.info.length * Gst.SECOND)
                        except (ValueError, TypeError): pass

                    if audio_raw.tags:
                        if isinstance(audio_raw.tags, mutagen.id3.ID3) and 'APIC:' in audio_raw.tags:
                            album_art_bytes = audio_raw.tags['APIC:'].data
                        elif isinstance(audio_raw, mutagen.mp4.MP4) and 'covr' in audio_raw.tags and audio_raw.tags['covr']:
                            album_art_bytes = bytes(audio_raw.tags['covr'][0])
                        elif hasattr(audio_raw, 'pictures') and audio_raw.pictures:
                            album_art_bytes = audio_raw.pictures[0].data

                    if album_art_bytes:
                        try:
                            album_art_glib_bytes = GLib.Bytes.new(album_art_bytes)
                        except Exception as wrap_e:
                            print(f"Sync Discover: Error wrapping album art bytes for {filepath}: {wrap_e}", file=sys.stderr)
                            album_art_glib_bytes = None
            except Exception as art_e:
                 print(f"Sync Discover: Mutagen error reading raw file/art tags from {filepath}: {art_e}", file=sys.stderr)

            if not album_art_glib_bytes:
                # Fallback to external art if no embedded art found
                album_art_glib_bytes = self._find_external_art(filepath)
                if album_art_glib_bytes:
                    print(f"Sync Discover: Found external art for {filepath}")

        except Exception as e:
            print(f"Sync Discover: General Mutagen error for {filepath}: {e}", file=sys.stderr)
            

        
        
        final_title = mutagen_title if mutagen_title else os.path.splitext(os.path.basename(filepath))[0]
        final_artist = mutagen_artist 
        final_album = mutagen_album
        
        duration_to_store = duration_ns if isinstance(duration_ns, int) and duration_ns >= 0 else 0

        
        try:
            song = Song(uri=uri, title=final_title, artist=final_artist, album=final_album, duration=duration_to_store)

            
            if album_art_glib_bytes:
                 song.album_art_data = album_art_glib_bytes

            
            return song
        except Exception as song_create_e:
             print(f"Sync Discover: Error creating Song object for {filepath}: {song_create_e}", file=sys.stderr)
             return None 

    
    def _discover_and_add_uri(self, uri):
        
        print(f"Starting ASYNC discovery for: {uri}") 
        self.discoverer.discover_uri_async(uri)

    def _on_discoverer_discovered(self, discoverer, info, error):
        """Callback when GstDiscoverer finishes discovering a URI."""
        
        print(f"--- ASYNC _on_discoverer_discovered called for URI: {info.get_uri()} ---") 
        uri = info.get_uri()

        
        if error:
            print(f"Error discovering URI: {uri} - {error.message}")
            if error.matches(Gst.DiscovererError.quark(), Gst.DiscovererError.URI_INVALID):
                print("Invalid URI.")
            elif error.matches(Gst.DiscovererError.quark(), Gst.DiscovererError.MISSING_PLUGIN):
                caps_struct = error.get_details() 
                if caps_struct:
                    print(f"Missing decoder for: {caps_struct.to_string()}")
                else:
                    print("Missing decoder details unavailable.")
            elif error.matches(Gst.DiscovererError.quark(), Gst.DiscovererError.MISC):
                print(f"Misc error: {error.message}")
            return 

        
        result = info.get_result()
        if result == GstPbutils.DiscovererResult.OK:
            gst_tags = info.get_tags()
            duration_ns = info.get_duration()

            
            mutagen_title = None
            mutagen_artist = None
            mutagen_album = None
            album_art_bytes = None
            album_art_glib_bytes = None 

            
            if uri.startswith('file://'):
                try:
                    filepath, _ = GLib.filename_from_uri(uri)
                    if not os.path.exists(filepath):
                        print(f"Mutagen error: File path does not exist: {filepath}", file=sys.stderr)
                    else:
                        print(f"Attempting to read tags/art with Mutagen: {filepath}")
                        
                        try:
                            audio_easy = mutagen.File(filepath, easy=True)
                            if audio_easy:
                                mutagen_title = audio_easy.get('title', [None])[0]
                                mutagen_artist = audio_easy.get('artist', [None])[0]
                                mutagen_album = audio_easy.get('album', [None])[0]
                        except Exception as tag_e:
                            print(f"Mutagen error reading easy tags from {filepath}: {tag_e}", file=sys.stderr)

                        # --- Embedded Art Priority ---
                        try:
                            print(f"Attempting Mutagen raw file load: {filepath}")
                            audio_raw = mutagen.File(filepath)
                            if audio_raw and audio_raw.tags:
                                print(f"Mutagen raw tags found: Type={type(audio_raw.tags)}")
                                if isinstance(audio_raw.tags, mutagen.id3.ID3): 
                                    if 'APIC:' in audio_raw.tags:
                                        print("Found APIC tag in ID3.")
                                        album_art_bytes = audio_raw.tags['APIC:'].data
                                    else:
                                        print("No APIC tag found in ID3.")
                                elif isinstance(audio_raw, mutagen.mp4.MP4): 
                                    if 'covr' in audio_raw.tags:
                                        artworks = audio_raw.tags['covr']
                                        if artworks:
                                            print("Found covr tag in MP4.")
                                            album_art_bytes = bytes(artworks[0])
                                        else:
                                            print("covr tag found but empty in MP4.")
                                    else:
                                        print("No covr tag found in MP4.")
                                elif hasattr(audio_raw, 'pictures') and audio_raw.pictures: 
                                    print(f"Found {len(audio_raw.pictures)} pictures in tags.")
                                    album_art_bytes = audio_raw.pictures[0].data
                                else:
                                    print("No known picture tag (APIC, covr, pictures) found.")

                                if album_art_bytes:
                                    try:
                                        print(f"Attempting to create GLib.Bytes from art ({len(album_art_bytes)} bytes).")
                                        album_art_glib_bytes = GLib.Bytes.new(album_art_bytes)
                                    except Exception as wrap_e:
                                        print(f"Error wrapping album art bytes: {wrap_e}", file=sys.stderr)
                                        album_art_glib_bytes = None 
                            else:
                                print(f"Mutagen could not find tags in raw file: {filepath}")
                        except Exception as art_e:
                             print(f"Mutagen error reading raw file/art tags from {filepath}: {art_e}", file=sys.stderr)

                        if not album_art_glib_bytes:
                            # Fallback to external art if no embedded art found
                            album_art_glib_bytes = self._find_external_art(filepath)
                            if album_art_glib_bytes:
                                print(f"Async Discover: Found external art for {filepath}")

                except Exception as e:
                    print(f"General Mutagen error for {uri}: {e}", file=sys.stderr)

            
            gst_title = gst_tags.get_string(Gst.TAG_TITLE)[1] if gst_tags and gst_tags.get_string(Gst.TAG_TITLE)[0] else None
            gst_artist = gst_tags.get_string(Gst.TAG_ARTIST)[1] if gst_tags and gst_tags.get_string(Gst.TAG_ARTIST)[0] else None
            gst_album = gst_tags.get_string(Gst.TAG_ALBUM)[1] if gst_tags and gst_tags.get_string(Gst.TAG_ALBUM)[0] else None

            
            final_title = mutagen_title if mutagen_title is not None else gst_title
            final_artist = mutagen_artist if mutagen_artist is not None else gst_artist
            final_album = mutagen_album if mutagen_album is not None else gst_album
            duration_to_store = duration_ns if isinstance(duration_ns, int) and duration_ns >= 0 else 0

            
            song_to_add = Song(uri=uri, title=final_title, artist=final_artist, album=final_album, duration=duration_to_store)

            
            if album_art_glib_bytes: 
                 try:
                     song_to_add.album_art_data = album_art_glib_bytes
                     print(f"Successfully assigned album art data to Song object for {final_title}")
                 except Exception as assign_e:
                      print(f"Error assigning album art GLib.Bytes: {assign_e}", file=sys.stderr)

            
            print(f"Discovered OK: URI='{song_to_add.uri}', Title='{song_to_add.title}', Artist='{song_to_add.artist}', Duration={song_to_add.duration / Gst.SECOND:.2f}s, Art Assigned={song_to_add.album_art_data is not None}")

            
            GLib.idle_add(self.playlist_store.append, song_to_add)
            print(f"Scheduled add for: {final_title or 'Unknown Title'}")

            # Start background waveform analysis
            self._start_waveform_analysis(song_to_add)

            # Auto-play if requested via internal flag OR menu setting
            auto_play_enabled = self.action_group.get_action_state("auto_play").get_boolean()
            if self._auto_play_after_load or auto_play_enabled:
                print(f"DISCOVERED: Queueing autoplay for '{song_to_add.title}' (Flag={self._auto_play_after_load}, Menu={auto_play_enabled})")
                GLib.idle_add(self._check_and_autoplay, song_to_add)
            else:
                print(f"DISCOVERED: Autoplay skipped for '{song_to_add.title}'")

        elif result == GstPbutils.DiscovererResult.TIMEOUT:
             print(f"Discovery Timeout: {uri}", file=sys.stderr)
        elif result == GstPbutils.DiscovererResult.BUSY:
             print(f"Discovery Busy: {uri} - Retrying later?", file=sys.stderr)
        elif result == GstPbutils.DiscovererResult.MISSING_PLUGINS:
             print(f"Discovery Missing Plugins: {uri}", file=sys.stderr)
        else:
             print(f"Discovery Result: {uri} - {result}", file=sys.stderr)

    def _check_and_autoplay(self, specific_song=None):
        """Checks if we should auto-play a song after load."""
        song_title = specific_song.title if specific_song else "None"
        print(f"AUTOPLAY START: song='{song_title}' current_song='{self.current_song.title if self.current_song else 'None'}'")
        # Safeguard: if we already have a song setting up or playing, don't interrupt.
        # This prevents the "leap-frogging" bug where multiple discovered songs
        # all try to start playback in rapid succession.
        if self.current_song is not None:
             print(f"_check_and_autoplay: current_song is already set ('{self.current_song.title}'), skipping.")
             self._auto_play_after_load = False
             return

        print(f"_check_and_autoplay: requested for specific_song='{specific_song.title if specific_song else 'None'}'")
        auto_play_enabled = self.action_group.get_action_state("auto_play").get_boolean()
        if self._auto_play_after_load or auto_play_enabled:
            # Also check player state as a backup
            if self.player and self.player.get_state(0).state == Gst.State.PLAYING:
                print("_check_and_autoplay: player is already PLAYING, skipping.")
                self._auto_play_after_load = False # Clear internal flag
                return

            target_song = None
            target_index = 0
            
            if specific_song:
                target_song = specific_song
                # Find index
                n = self.playlist_store.get_n_items()
                # fast check last
                if n > 0 and self.playlist_store.get_item(n-1) == specific_song:
                     target_index = n - 1
                else:
                     # iterate
                     for i in range(n):
                         if self.playlist_store.get_item(i) == specific_song:
                             target_index = i
                             break
            elif self.playlist_store.get_n_items() > 0:
                target_song = self.playlist_store.get_item(0)
                target_index = 0
            
            if target_song and target_song.uri:
                print(f"Auto-playing song: {target_song.title}")
                self._auto_play_after_load = False
                self.selection_model.set_selected(target_index)
                self.play_uri(target_song.uri)


    def _on_discoverer_finished(self, discoverer):
        print("--- _on_discoverer_finished called ---") 

    def _on_player_message(self, bus, message):
        """Handles messages from the GStreamer bus."""
        t = message.type
        if t == Gst.MessageType.ERROR:
            err, dbg = message.parse_error()
            print(f"ERROR: {err.message} ({dbg})", file=sys.stderr)
            
            if self.player:
                self.player.set_state(Gst.State.NULL)
                self.play_pause_button.set_icon_name(self.PLAY_ICON)
                self.waveform.set_fraction(0.0)
                self.waveform.set_sensitive(False)
                self.current_song = None
                self._update_song_display(None)
        elif t == Gst.MessageType.EOS:
            print("End-of-stream reached.")

            # Check repeat state
            repeat = self.action_group.get_action_state("repeat").get_boolean()
            if repeat and self.current_song:
                print("Repeat enabled. Replaying current song.")
                if self.player:
                    self.player.set_state(Gst.State.NULL)
                    self.play_uri(self.current_song.uri)
                return

            if self.player:
                self.player.set_state(Gst.State.NULL) 
                self.play_pause_button.set_icon_name(self.PLAY_ICON)
                self.waveform.set_fraction(0.0)
                self.waveform.set_sensitive(False)
                self.current_song = None
                self._update_song_display(None)
                
                # Auto-play next if enabled
                auto_play_enabled = self.action_group.get_action_state("auto_play").get_boolean()
                if auto_play_enabled:
                    print("EOS: Autoplay enabled. Selecting next song.")
                    GLib.idle_add(self._on_next_clicked) 
                else:
                    print("EOS: Autoplay disabled. Stopping.")
        elif t == Gst.MessageType.ELEMENT:
            if self._level_elem and message.src == self._level_elem:
                if not self.current_song:
                    return True

                struct = message.get_structure()
                if struct and struct.get_name() == "level":
                    rms_list = struct.get_value("rms")
                    if rms_list:
                        avg_db = sum(rms_list) / len(rms_list)
                        linear = pow(10, avg_db / 20.0)

                        if self.current_song.waveform_data is not None and len(self.current_song.waveform_data) > 0:
                            # Use pre-generated or already fully populated data
                            return True

                        if self.current_song.waveform_data is None:
                            self.current_song.waveform_data = []

                        MAX_WAVEFORM_SAMPLES = 3000

                        if len(self.current_song.waveform_data) < MAX_WAVEFORM_SAMPLES:
                            self.current_song.waveform_data.append(linear)

                        if not hasattr(self, "_waveform_push_ctr"):
                            self._waveform_push_ctr = 0
                        self._waveform_push_ctr += 1

                        # Update UI every ~2 seconds (8 * 250ms)
                        if self._waveform_push_ctr % 8 == 0:
                            data = list(self.current_song.waveform_data)
                            GLib.idle_add(lambda d=data: self.waveform.set_waveform_data(d))

        elif t == Gst.MessageType.STATE_CHANGED:
            old_state, new_state, pending_state = message.parse_state_changed()
            
            if message.src == self.player:
                print(f"State changed from {old_state.value_nick} to {new_state.value_nick}")
                if new_state == Gst.State.PLAYING:
                    self.waveform.set_sensitive(True)
                    self.play_pause_button.set_icon_name(self.PAUSE_ICON)
                    
                    
                    if not hasattr(self, '_progress_timer_id') or self._progress_timer_id is None:
                         self._progress_timer_id = GLib.timeout_add_seconds(1, self._update_progress)
                    
                
                elif new_state == Gst.State.PAUSED:
                    self.play_pause_button.set_icon_name(self.PLAY_ICON)
                    
                elif new_state == Gst.State.READY or new_state == Gst.State.NULL:
                    self.play_pause_button.set_icon_name(self.PLAY_ICON)
                    self.waveform.set_fraction(0.0)
                    self.waveform.set_sensitive(False)
                    
                    if hasattr(self, '_progress_timer_id') and self._progress_timer_id is not None:
                        GLib.source_remove(self._progress_timer_id)
                        self._progress_timer_id = None
                
                self.mpris.update_playback_status()
        elif t == Gst.MessageType.DURATION_CHANGED:
             self.duration_ns = self.player.query_duration(Gst.Format.TIME)[1]
             print(f"Duration changed: {self.duration_ns / Gst.SECOND:.2f}s")
             self._update_progress()


        
        return True
    def _update_song_display(self, song):
        """Updates the song title, artist, time label (0:00 / Duration), and cover art."""
        # Always reset to placeholder first to prevent persistence of old artwork
        self.cover_image.set_from_icon_name("audio-x-generic-symbolic")
        self.cover_image.set_visible(True)

        # Update is_playing state in playlist - O(N) but better than old loop if we only touch changed items
        # Fast path: only reset previous and set new
        if self._last_indicated_song:
             self._last_indicated_song.is_playing = False
        
        if self.current_song:
             # Find the actual object in the store if it's different (e.g. from reload)
             # but usually it's the same object. 
             # Let's just set the property on current_song and hope it's the one in the store.
             self.current_song.is_playing = True
             self._last_indicated_song = self.current_song
        else:
             self._last_indicated_song = None

        if song:
            # Ensure background analysis starts if missing
            self._start_waveform_analysis(song)
            
            self.song_label.set_label(song.title)
            self.song_label.set_tooltip_text(song.title)
            
            self.artist_label.set_label(song.artist or "Unknown Artist")

            
            duration_ns = song.duration
            if duration_ns is not None and duration_ns != Gst.CLOCK_TIME_NONE and duration_ns > 0:
                dur_sec = duration_ns // Gst.SECOND
                duration_str = f"{dur_sec // 60}:{dur_sec % 60:02d}"
            else:
                duration_str = "--:--"
            self.time_label_current.set_label("0:00")
            self.time_label_remaining.set_label("--:--")

            
            glib_bytes_data = song.album_art_data
            
            if glib_bytes_data:
                raw_bytes_data = glib_bytes_data.get_data() 
                try:
                    loader = GdkPixbuf.PixbufLoader()
                    loader.write(raw_bytes_data)
                    loader.close()
                    pixbuf = loader.get_pixbuf()
                    
                    scaled_pixbuf = pixbuf.scale_simple(256, 256, GdkPixbuf.InterpType.HYPER)
                    self.cover_image.set_from_pixbuf(scaled_pixbuf)
                    
                except Exception as e:
                    print(f"Error loading album art in _update_song_display for '{song.title}': {e}", file=sys.stderr)
                    # Already set to generic icon above
            
            if song.waveform_data:
                self.waveform.set_waveform_data(song.waveform_data)
            else:
                self.waveform.set_waveform_data([])
            
        else:
            
            
            self.song_label.set_label("<No Song Playing>")
            self.song_label.set_tooltip_text("") 
            self.artist_label.set_label("")
            self.time_label_current.set_label("0:00")
            self.time_label_remaining.set_label("-0:00")
            self.waveform.set_waveform_data([])

    def _start_waveform_analysis(self, song):
        """Adds a song to the background analysis queue."""
        if song.waveform_data or song.uri in self._active_analysis_uris:
            return

        # Check cache first
        cached_data = self._load_waveform_from_cache(song)
        if cached_data:
            song.waveform_data = cached_data
            if self.current_song == song:
                GLib.idle_add(lambda: self.waveform.set_waveform_data(song.waveform_data))
            return
            
        self._active_analysis_uris.add(song.uri)
        self._analysis_queue.append(song)
        
        if not self._analysis_worker_running:
            self._analysis_worker_running = True
            thread = threading.Thread(target=self._analysis_worker_loop, daemon=True)
            thread.start()

    def _analysis_worker_loop(self):
        """Worker thread that processes the analysis queue one by one."""
        while self._analysis_queue:
            song = self._analysis_queue.popleft()
            try:
                self._analyze_waveform_thread(song)
            except Exception as e:
                print(f"Analysis worker error: {e}")
        self._analysis_worker_running = False

    def _analyze_waveform_thread(self, song):
        """Background thread to scan the audio file and generate waveform data."""
        uri = song.uri
        # Use uridecodebin and level for fast scanning. 50ms interval for good detail.
        pipeline_str = f"uridecodebin uri=\"{uri}\" ! audioconvert ! level interval=50000000 post-messages=true ! fakesink"
        try:
            pipeline = Gst.parse_launch(pipeline_str)
            if not pipeline:
                print(f"Failed to create analysis pipeline for {uri}")
                self._active_analysis_uris.discard(uri)
                return

            bus = pipeline.get_bus()
            pipeline.set_state(Gst.State.PLAYING)
            
            waveform_data = []
            
            while True:
                msg = bus.timed_pop_filtered(Gst.CLOCK_TIME_NONE, 
                                           Gst.MessageType.EOS | Gst.MessageType.ERROR | Gst.MessageType.ELEMENT)
                if not msg:
                    break
                
                t = msg.type
                if t == Gst.MessageType.EOS:
                    break
                elif t == Gst.MessageType.ERROR:
                    err, dbg = msg.parse_error()
                    print(f"Waveform Analysis Error for {uri}: {err.message}")
                    break
                elif t == Gst.MessageType.ELEMENT:
                    struct = msg.get_structure()
                    if struct and struct.get_name() == "level":
                        rms_list = struct.get_value("rms")
                        if rms_list:
                            avg_db = sum(rms_list) / len(rms_list)
                            linear = pow(10, avg_db / 20.0)
                            waveform_data.append(linear)
            
            pipeline.set_state(Gst.State.NULL)
            
            if waveform_data:
                song.waveform_data = waveform_data
                GLib.idle_add(self._on_waveform_analysis_finished, song)
            
            self._active_analysis_uris.discard(uri)
                
        except Exception as e:
            print(f"Unexpected error in waveform analysis for {uri}: {e}")
            if hasattr(self, "_active_analysis_uris"):
                self._active_analysis_uris.discard(uri)

    def _on_waveform_analysis_finished(self, song):
        """Triggered on main thread when analysis is done."""
        if song.waveform_data:
            self._save_waveform_to_cache(song, song.waveform_data)

        if self.current_song == song:
            self.waveform.set_waveform_data(song.waveform_data)
        self._schedule_playlist_save()

    def _schedule_playlist_save(self):
        """Schedules a playlist save with debouncing (2 seconds)."""
        if self._save_timer_id is not None:
            GLib.source_remove(self._save_timer_id)
        self._save_timer_id = GLib.timeout_add_seconds(2, self._debounced_save)

    def _debounced_save(self):
        """Timer callback to perform the actual save."""
        self._save_timer_id = None
        self._save_playlist()
        return False

    def _get_song_hash(self, song):
        """Generates a SHA256 hash for the song URI to use as a cache key."""
        return hashlib.sha256(song.uri.encode('utf-8')).hexdigest()

    def _load_waveform_from_cache(self, song):
        """Attempts to load waveform data from the disk cache."""
        song_hash = self._get_song_hash(song)
        cache_path = os.path.join(self._waveform_cache_dir, f"{song_hash}.json")
        
        if os.path.exists(cache_path):
            try:
                with open(cache_path, 'r') as f:
                    return json.load(f)
            except Exception as e:
                print(f"Error loading waveform cache for {song.uri}: {e}")
        return None

    def _save_waveform_to_cache(self, song, data):
        """Saves waveform data to the disk cache."""
        if not data:
            return
        song_hash = self._get_song_hash(song)
        cache_path = os.path.join(self._waveform_cache_dir, f"{song_hash}.json")
        try:
            with open(cache_path, 'w') as f:
                json.dump(data, f)
        except Exception as e:
            print(f"Error saving waveform cache for {song.uri}: {e}")


    
    

    

    def _update_progress(self):
        """Timer callback to update playback progress."""
        if not self.player or not self.current_song:
            
            if hasattr(self, '_progress_timer_id') and self._progress_timer_id is not None:
                GLib.source_remove(self._progress_timer_id)
                self._progress_timer_id = None
            return False 

        state = self.player.get_state(0).state
        if state != Gst.State.PLAYING and state != Gst.State.PAUSED:
            
            self._progress_timer_id = None
            return False

        
        
        if self.duration_ns <= 0:
             ok, new_duration_ns = self.player.query_duration(Gst.Format.TIME)
             if ok:
                 self.duration_ns = new_duration_ns 
             else:
                 print("Could not query duration in timer.")
                 self.duration_ns = 0 
        
        ok_pos, position_ns = self.player.query_position(Gst.Format.TIME)
        if self.duration_ns > 0:
             fraction = position_ns / self.duration_ns
             self.waveform.set_fraction(fraction)
        
        # Update Time Labels
        # Current: MM:SS
        pos_sec = position_ns // Gst.SECOND
        current_str = f"{pos_sec // 60}:{pos_sec % 60:02d}"
        self.time_label_current.set_label(current_str)

        # Remaining: -MM:SS
        if self.duration_ns > 0:
            rem_ns = self.duration_ns - position_ns
            rem_sec = rem_ns // Gst.SECOND
            rem_str = f"-{rem_sec // 60}:{rem_sec % 60:02d}"
            self.time_label_remaining.set_label(rem_str)

        return True 

    def _on_waveform_seek(self, fraction):
        """Called when user clicks/drags on the waveform to seek."""
        if not self.player or self.duration_ns <= 0:
            return
            
        target_ns = int(fraction * self.duration_ns)
        print(f"Seeking to {target_ns / Gst.SECOND:.2f}s")
        
        seek_flags = Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT
        self.player.seek_simple(Gst.Format.TIME, seek_flags, target_ns)


    

    def _on_prev_clicked(self, button):
        """Handles the Previous button click."""
        if not self.player: return

        can_seek, position_ns = self.player.query_position(Gst.Format.TIME)
        state = self.player.get_state(0).state

        # If playing past 3 seconds, seek to start
        if state in (Gst.State.PLAYING, Gst.State.PAUSED) and can_seek and position_ns > (3 * Gst.SECOND):
            print("Previous: Seeking to beginning.")
            seek_flags = Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT
            self.player.seek_simple(Gst.Format.TIME, seek_flags, 0)
        else:
            print("Previous: Selecting previous track.")
            current_pos = self.selection_model.get_selected()
            n_items = self.playlist_store.get_n_items()
            if n_items == 0: return

            new_pos = Gtk.INVALID_LIST_POSITION
            if current_pos != Gtk.INVALID_LIST_POSITION and current_pos > 0:
                new_pos = current_pos - 1
            elif (current_pos == 0 or current_pos == Gtk.INVALID_LIST_POSITION) and n_items > 0:
                loop_all = self.action_group.get_action_state("loop_all").get_boolean()
                if loop_all or current_pos == Gtk.INVALID_LIST_POSITION:
                    new_pos = n_items - 1
            
            if new_pos != Gtk.INVALID_LIST_POSITION:
                self.selection_model.set_selected(new_pos)
                song = self.playlist_store.get_item(new_pos)
                if song and song.uri:
                    self.play_uri(song.uri)
            

    def _on_next_clicked(self, button=None): 
        """Handles the Next button click or auto-plays next song."""
        print("Next: Selecting next track.")
        n_items = self.playlist_store.get_n_items()
        if n_items == 0: return 

        current_pos = self.selection_model.get_selected()
        new_pos = Gtk.INVALID_LIST_POSITION

        if current_pos != Gtk.INVALID_LIST_POSITION and current_pos < (n_items - 1):
            new_pos = current_pos + 1
        elif (current_pos == (n_items - 1) or current_pos == Gtk.INVALID_LIST_POSITION) and n_items > 0:
            loop_all = self.action_group.get_action_state("loop_all").get_boolean()
            if loop_all or current_pos == Gtk.INVALID_LIST_POSITION:
                new_pos = 0
            else:
                print("End of playlist reached, loop_all is OFF.")
        
        if new_pos != Gtk.INVALID_LIST_POSITION:
            self.selection_model.set_selected(new_pos)
            song = self.playlist_store.get_item(new_pos)
            if song and song.uri:
                self.play_uri(song.uri)
        

    
    def _on_open_playlist_action(self, action, param): 
        """Handles the 'win.open_playlist' action."""
        dialog = Gtk.FileDialog.new()
        dialog.set_title("Open Playlist")
        dialog.set_modal(True)

        
        json_filter = Gtk.FileFilter.new()
        json_filter.set_name("Playlist Files (*.json)")
        json_filter.add_mime_type("application/json")
        json_filter.add_pattern("*.json")

        
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(json_filter)
        dialog.set_filters(filters)
        dialog.set_default_filter(json_filter) 

        dialog.open(parent=self, cancellable=None, callback=self._on_open_dialog_finish)

    def _on_open_dialog_finish(self, dialog, result):
        """Callback after the open file dialog closes."""
        try:
            gio_file = dialog.open_finish(result)
            if gio_file:
                filepath = gio_file.get_path()
                print(f"Opening playlist from: {filepath}")
                
                self.playlist_store.remove_all()
                self._load_playlist(filepath=filepath)
        except GLib.Error as e:
            if e.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED):
                print("Open playlist cancelled.")
            else:
                print(f"Error opening playlist file: {e.message}", file=sys.stderr)
                

    def _on_save_playlist_action(self, action, param): 
        """Handles the 'win.save_playlist' action."""
        dialog = Gtk.FileDialog.new()
        dialog.set_title("Save Playlist As")
        dialog.set_modal(True)
        dialog.set_initial_name("playlist.json")

        
        json_filter = Gtk.FileFilter.new()
        json_filter.set_name("Playlist Files (*.json)")
        json_filter.add_mime_type("application/json")
        json_filter.add_pattern("*.json")

        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(json_filter)
        dialog.set_filters(filters)
        dialog.set_default_filter(json_filter)

        
        try:
            home_dir = GLib.get_home_dir()
            if home_dir:
                 initial_folder_file = Gio.File.new_for_path(home_dir)
                 dialog.set_initial_folder(initial_folder_file)
        except Exception as e:
            print(f"Could not set initial folder for save dialog: {e}")


        dialog.save(parent=self, cancellable=None, callback=self._on_save_dialog_finish)

    def _on_save_dialog_finish(self, dialog, result):
        """Callback after the save file dialog closes."""
        try:
            gio_file = dialog.save_finish(result)
            if gio_file:
                filepath = gio_file.get_path()
                
                if not filepath.lower().endswith(".json"):
                    filepath += ".json"
                    print(f"Appended .json extension. Saving to: {filepath}")
                else:
                    print(f"Saving playlist to: {filepath}")
                self._save_playlist(filepath=filepath)
        except GLib.Error as e:
            if e.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED):
                print("Save playlist cancelled.")
            else:
                print(f"Error saving playlist file: {e.message}", file=sys.stderr)
                

    
    def _on_add_file_action(self, action, param):
        """Handles the 'win.add_file' action."""
        self._auto_play_after_load = False
        self._show_file_dialog()

    def _on_add_folder_action(self, action, param): 
        """Handles the 'win.add_folder_new' action."""
        dialog = Gtk.FileDialog.new()
        dialog.set_title("Select Folder(s) to Add")
        dialog.set_modal(True)
        

        print("Opening folder selection dialog...")
        dialog.select_multiple_folders(parent=self, cancellable=None,
                                     callback=self._on_select_multiple_folders_finish)

    def _on_select_multiple_folders_finish(self, dialog, result):
        """Callback after the select_multiple_folders dialog closes."""
        try:
            folders = dialog.select_multiple_folders_finish(result)
            if folders:
                n_folders = folders.get_n_items()
                print(f"Folders selected: {n_folders}")
                for i in range(n_folders):
                    folder_file = folders.get_item(i) 
                    if folder_file:
                        print(f"Processing selected folder: {folder_file.get_path()}")
                        self._start_folder_scan(folder_file) 
                    else:
                        print(f"Warning: Got null folder item at index {i}")
            else:
                
                print("No folders selected or dialog closed unexpectedly.")

        except GLib.Error as e:
            
            if e.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED):
                print("Folder selection cancelled.")
            else:
                
                print(f"Error selecting folders: {e.message}", file=sys.stderr)
                
        except Exception as general_e: 
             print(f"Unexpected error during folder selection finish: {general_e}", file=sys.stderr)

    


    def _on_about_action(self, action, param): 
        """Handles the 'win.about' action."""
        about_window = Adw.AboutWindow()
        about_window.set_transient_for(self)
        about_window.set_application_name("Mamo Music Player")
        about_window.set_application_icon("audio-x-generic")
        about_window.set_version("0.1.0") 
        about_window.set_developer_name("") 
        about_window.set_copyright("© 2025 Robert Renling, 2026 David Philip Barr") 
        about_window.set_developers(["Robert Renling", "David Barr", "hat tipped in the direction of Jorn Baayen."]) 
        about_window.set_license_type(Gtk.License.CUSTOM) 
        about_window.set_license("Mamo is licensed under the GPL v2.") 
        about_window.set_website("") 
        about_window.set_issue_url("") 

        about_window.present()


    
    def _load_settings(self):
        """Loads app settings from a JSON file."""
        if os.path.exists(self._settings_file_path):
            try:
                with open(self._settings_file_path, 'r') as f:
                    settings = json.load(f)
                    dark_mode = settings.get("dark_mode", False)
                    # Update action state
                    action = self.action_group.lookup_action("dark_mode")
                    if action:
                        action.change_state(GLib.Variant.new_boolean(dark_mode))
                    # Apply style
                    self._apply_dark_mode(dark_mode)

                    auto_play = settings.get("auto_play", True)
                    ap_action = self.action_group.lookup_action("auto_play")
                    if ap_action:
                        ap_action.change_state(GLib.Variant.new_boolean(auto_play))

                    clear_on_start = settings.get("clear_on_start", False)
                    cos_action = self.action_group.lookup_action("clear_on_start")
                    if cos_action:
                        cos_action.change_state(GLib.Variant.new_boolean(clear_on_start))

                    repeat_val = settings.get("repeat", False)
                    rep_action = self.action_group.lookup_action("repeat")
                    if rep_action:
                        rep_action.change_state(GLib.Variant.new_boolean(repeat_val))

                    loop_all_val = settings.get("loop_all", False)
                    la_action = self.action_group.lookup_action("loop_all")
                    if la_action:
                        la_action.change_state(GLib.Variant.new_boolean(loop_all_val))

                    self.library_path = settings.get("library_path", os.path.expanduser("~/Music"))

            except Exception as e:
                print(f"Error loading settings: {e}", file=sys.stderr)

    def _save_settings(self):
        """Saves app settings to a JSON file."""
        os.makedirs(os.path.dirname(self._settings_file_path), exist_ok=True)
        settings = {
            "dark_mode": self.action_group.get_action_state("dark_mode").get_boolean(),
            "auto_play": self.action_group.get_action_state("auto_play").get_boolean(),
            "clear_on_start": self.action_group.get_action_state("clear_on_start").get_boolean(),
            "repeat": self.action_group.get_action_state("repeat").get_boolean(),
            "loop_all": self.action_group.get_action_state("loop_all").get_boolean(),
            "library_path": self.library_path
        }
        try:
            with open(self._settings_file_path, 'w') as f:
                json.dump(settings, f)
        except Exception as e:
            print(f"Error saving settings: {e}", file=sys.stderr)

    def _on_clear_on_start_action_activated(self, action, parameter):
        """Toggles 'clear on start' setting and saves it."""
        state = action.get_state().get_boolean()
        new_state = not state
        action.change_state(GLib.Variant.new_boolean(new_state))
        self._save_settings()

    def _on_dark_mode_action_activated(self, action, parameter):
        """Toggles dark mode and saves the setting."""
        state = action.get_state().get_boolean()
        new_state = not state
        action.change_state(GLib.Variant.new_boolean(new_state))
        self._apply_dark_mode(new_state)
        self._save_settings()

    def _on_auto_play_action_activated(self, action, parameter):
        """Toggles auto play setting."""
        state = action.get_state().get_boolean()
        new_state = not state
        action.change_state(GLib.Variant.new_boolean(new_state))
        print(f"Auto Play toggled to: {new_state}")
        self._save_settings()

    def _on_repeat_action_activated(self, action, parameter):
        """Toggles repeat setting."""
        state = action.get_state().get_boolean()
        new_state = not state
        action.change_state(GLib.Variant.new_boolean(new_state))
        print(f"Repeat toggled to: {new_state}")
        self._save_settings()

    def _on_loop_all_action_activated(self, action, parameter):
        """Toggles loop all setting."""
        state = action.get_state().get_boolean()
        new_state = not state
        action.change_state(GLib.Variant.new_boolean(new_state))
        print(f"Loop All toggled to: {new_state}")
        self._save_settings()
        self._update_playback_controls_sensitivity()

    def _apply_dark_mode(self, enabled):
        """Applies dark mode using Libadwaita StyleManager."""
        style_manager = Adw.StyleManager.get_default()
        if enabled:
            style_manager.set_color_scheme(Adw.ColorScheme.PREFER_DARK)
        else:
            style_manager.set_color_scheme(Adw.ColorScheme.PREFER_LIGHT)

    def _load_playlist(self, filepath=None):
        """Loads the playlist from a JSON file in a background thread."""
        path_to_use = filepath if filepath else self._playlist_file_path

        if not os.path.exists(path_to_use):
            if filepath: 
                print(f"Error: Playlist file not found: {path_to_use}", file=sys.stderr)
            else:
                 print("Default playlist file not found, starting empty.")
            return

        self._is_loading = True
        self._update_viewport()

        def background_load():
            songs_to_add = []
            try:
                print(f"Loading playlist from: {path_to_use}")
                with open(path_to_use, 'r') as f:
                    playlist_data = json.load(f)

                if isinstance(playlist_data, list):
                    for item in playlist_data:
                        if isinstance(item, dict):
                            duration_ns_loaded = item.get('duration_ns', 0)
                            if not isinstance(duration_ns_loaded, int) or duration_ns_loaded < 0:
                                duration_ns_loaded = 0

                            album_art_glib_bytes = None
                            album_art_b64 = item.get('album_art_b64')
                            if album_art_b64:
                                try:
                                    decoded_bytes = base64.b64decode(album_art_b64)
                                    album_art_glib_bytes = GLib.Bytes.new(decoded_bytes)
                                except Exception: pass

                            song = Song(uri=item.get('uri'),
                                         title=item.get('title'),
                                         artist=item.get('artist'),
                                         duration=duration_ns_loaded)
                            
                            if album_art_glib_bytes:
                                song.album_art_data = album_art_glib_bytes
                            
                            song.waveform_data = self._load_waveform_from_cache(song)
                            if not song.waveform_data:
                                song.waveform_data = item.get('waveform_data')
                            
                            songs_to_add.append(song)
            except Exception as e:
                print(f"Error in background playlist load: {e}", file=sys.stderr)
            
            GLib.idle_add(self._apply_loaded_playlist, songs_to_add)

        thread = threading.Thread(target=background_load, daemon=True)
        thread.start()

    def _apply_loaded_playlist(self, songs):
        """Called on main thread to populate the playlist store."""
        # Only remove all if it's a full playlist load (we might want to change this)
        self.playlist_store.remove_all()
        for s in songs:
            self.playlist_store.append(s)
        self._is_loading = False
        self._update_viewport()
        self._update_viewport()
        
        # We NO LONGER populate the Now Playing UI here by default,
        # to ensure it stays in sync with current_song (which is None).
        self._update_song_display(None)
        self.selection_model.set_selected(0)

        # Trigger background repair for 0-duration items
        threading.Thread(target=self._repair_playlist_durations, daemon=True).start()

    def _repair_playlist_durations(self):
        """Background thread to fix missing durations in the playlist."""
        needs_save = False
        n_items = self.playlist_store.get_n_items()
        
        for i in range(n_items):
            song = self.playlist_store.get_item(i)
            if song and (song.duration is None or song.duration == 0):
                path = self._uri_to_path(song.uri)
                if path and os.path.exists(path):
                    try:
                        # Try Mutagen Easy
                        audio = mutagen.File(path, easy=True)
                        if audio and audio.info and hasattr(audio.info, 'length'):
                            song.duration = int(audio.info.length * Gst.SECOND)
                            needs_save = True
                        # Fallback to standard Mutagen
                        elif not audio:
                             audio = mutagen.File(path)
                             if audio and audio.info and hasattr(audio.info, 'length'):
                                 song.duration = int(audio.info.length * Gst.SECOND)
                                 needs_save = True
                    except Exception as e:
                        print(f"Error repairing duration for {path}: {e}")

            # Trigger waveform analysis if missing
            if song and song.duration > 0 and not song.waveform_data:
                self._start_waveform_analysis(song)

        if needs_save:
            GLib.idle_add(self._schedule_playlist_save)

    def _uri_to_path(self, uri):
        try:
            parsed = urlparse(uri)
            return unquote(parsed.path)
        except:
            return None

    def _save_playlist(self, filepath=None):
        """Saves the current playlist to a JSON file. Uses default if filepath is None."""
        path_to_use = filepath if filepath else self._playlist_file_path
        playlist_data = []
        for i in range(self.playlist_store.get_n_items()):
            song = self.playlist_store.get_item(i)
            
            duration_to_save = song.duration if isinstance(song.duration, int) and song.duration >= 0 else 0

            
            song_data_to_save = {
                'uri': song.uri,
                'title': song.title,
                'artist': song.artist,
                'duration_ns': duration_to_save
            }
            
            if song.album_art_data:
                 raw_bytes = song.album_art_data.get_data()
                 song_data_to_save['album_art_b64'] = base64.b64encode(raw_bytes).decode('ascii')

            
            playlist_data.append(song_data_to_save)

        try:
            
            target_dir = os.path.dirname(path_to_use)
            if target_dir: 
                 os.makedirs(target_dir, exist_ok=True)

            print(f"Saving playlist to: {path_to_use}")
            with open(path_to_use, 'w') as f:
                json.dump(playlist_data, f, indent=2) 

        except Exception as e:
            print(f"Error saving playlist to {path_to_use}: {e}", file=sys.stderr)

    



class MamoApplication(Adw.Application):
    def __init__(self, **kwargs):
        super().__init__(application_id='org.broomlabs.MamoMusicPlayer',
                         flags=Gio.ApplicationFlags.HANDLES_OPEN,
                        **kwargs)
        GLib.set_prgname("mamo")
        GLib.set_application_name("Mamo")
        Gtk.Window.set_default_icon_name("multimedia-audio-player")
        self.window = None

    def do_activate(self):
        if not self.window:
            self.window = MamoWindow(application=self)
        self.window.present()

    def do_open(self, files, n_files, hint):
        """Handles opening files and folders from the system."""
        if not self.window:
            self.window = MamoWindow(application=self)
        self.window.present()
        
        # Set autoplay if these are the first files being added
        is_empty = self.window.playlist_store.get_n_items() == 0
        if is_empty:
            self.window._auto_play_after_load = True

        for i in range(n_files):
            f = files[i]
            try:
                info = f.query_info(Gio.FILE_ATTRIBUTE_STANDARD_TYPE, Gio.FileQueryInfoFlags.NONE, None)
                if info.get_file_type() == Gio.FileType.DIRECTORY:
                    self.window._start_folder_scan(f)
                else:
                    self.window._discover_and_add_uri(f.get_uri())
            except Exception as e:
                print(f"Error handling file '{f.get_uri()}': {e}", file=sys.stderr)

    def do_startup(self):
        Adw.Application.do_startup(self)

        provider = Gtk.CssProvider()
        provider.load_from_data(STYLE_CSS.encode('utf-8'))
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

    def do_shutdown(self):
        
        if self.window:
             self.window._save_playlist()

        
        if self.window and hasattr(self.window, 'discoverer') and self.window.discoverer:
            print("Stopping discoverer...")
            self.window.discoverer.stop()
        if self.window and hasattr(self.window, 'player') and self.window.player:
             print("Setting player to NULL state...")
             self.window.player.set_state(Gst.State.NULL)

        Adw.Application.do_shutdown(self)


def main():
    
    Gst.init(None)
    
    app = MamoApplication()
    return app.run(sys.argv)

if __name__ == "__main__":
    sys.exit(main())
