
import sys
import threading
import collections
import os
import random
import subprocess
import tempfile
import html
import json
import base64
import mutagen
import pathlib
from urllib.parse import urlparse, unquote
import hashlib

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
gi.require_version('Gst', '1.0')
gi.require_version('GstPbutils', '1.0')
gi.require_version('GdkPixbuf', '2.0')

from gi.repository import Gtk, Adw, Gio, GLib, GObject, Gst, GstPbutils, GdkPixbuf, Gdk, Pango

from ..models import Song
from ..library import LibraryManager
from ..mpris import MprisManager
from .widgets import WaveformBar
from .browser import AlbumBrowser

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
        self.toast_overlay = Adw.ToastOverlay()
        self.set_content(self.toast_overlay)

        self.toolbar_view = Adw.ToolbarView()
        self.toolbar_view.add_css_class("main-toolbar-view")
        self.toast_overlay.set_child(self.toolbar_view)

        # Dynamic CSS provider for album art tinting
        self._dynamic_tint_provider = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            self._dynamic_tint_provider,
            Gtk.STYLE_PROVIDER_PRIORITY_USER
        )

        header = Adw.HeaderBar.new()
        header.add_css_class("main-header-bar")
        
        # Use a static title widget so the visible title stays "Mamo" regardless of window title updates
        self.header_title = Adw.WindowTitle(title="Mamo", subtitle="")
        header.set_title_widget(self.header_title)
        
        self.toolbar_view.add_top_bar(header) 

        # Main content stack (Status Page vs Playlist)
        self.main_stack = Gtk.Stack()
        self.main_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.toolbar_view.set_content(self.main_stack)

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
        main_box.add_css_class("main-player-box")
        self._main_player_box = main_box
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
        
        main_menu.append(_("Open Playlist"), "win.open_playlist")
        main_menu.append(_("Save Playlist"), "win.save_playlist")
        main_menu.append(_("Add Folder..."), "win.add_folder_new") 
        main_menu.append(_("Add Files..."), "win.add_file")
        main_menu.append(_("Clear Playlist"), "win.clear_playlist")
        
        section = Gio.Menu()
        section.append(_("Use Dark Mode"), "win.dark_mode")
        section.append(_("Use Album Tinting"), "win.album_tinting")
        section.append(_("Auto Play"), "win.auto_play")
        section.append(_("Loop All"), "win.loop_all")
        section.append(_("Clear Playlist on Start"), "win.clear_on_start")
        section.append(_("About"), "win.about")
        main_menu.append_section(None, section)

        menu_button = Gtk.MenuButton.new()
        menu_button.set_icon_name("open-menu-symbolic")
        menu_button.set_menu_model(main_menu) 

        play_album_button = Gtk.Button.new_from_icon_name("folder-music-symbolic")
        play_album_button.connect("clicked", self._on_play_album_clicked)
        play_album_button.set_tooltip_text(_("Album Browser"))

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


        playlist_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        playlist_container.set_margin_start(12)
        playlist_container.set_margin_end(12)
        playlist_container.set_margin_bottom(12)
        playlist_container.add_css_class("main-playlist-card")
        main_box.append(playlist_container)

        scrolled_window = Gtk.ScrolledWindow()
        scrolled_window.set_vexpand(True)
        scrolled_window.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled_window.add_css_class("playlist-view-scrolled")
        playlist_container.append(scrolled_window)

        
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
        self.playlist_view.set_show_separators(True)
        self.playlist_view.add_css_class("playlist-view")
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

        # Stateful album tinting action
        album_tinting_action = Gio.SimpleAction.new_stateful("album_tinting", None, GLib.Variant.new_boolean(True))
        album_tinting_action.connect("activate", self._on_album_tinting_action_activated)
        action_group.add_action(album_tinting_action)

        # Show file location action
        show_loc_action = Gio.SimpleAction.new("show_file_location", GLib.VariantType.new("s"))
        show_loc_action.connect("activate", self._on_show_file_location)
        action_group.add_action(show_loc_action)

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
            
            # Capture old song to clear its state later
            old_song = self.current_song
            
            self.current_song = None
            n = self.playlist_store.get_n_items()
            for i in range(n):
                 song = self.playlist_store.get_item(i)
                 if song.uri == uri:
                     self.current_song = song
                     break
            
            # Clear state of old song if it's different
            if old_song and old_song != self.current_song:
                old_song.is_playing = False

            self._update_song_display(self.current_song)
            self.mpris.update_metadata(self.current_song)
            self.mpris.update_playback_status()
            
            print(f"Playing URI: {uri}")
            # FORCE STOP before changing URI to ensure switch happens
            self.player.set_state(Gst.State.NULL)
            self.player.set_property("uri", uri)
            self.player.set_state(Gst.State.PLAYING)
            
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
        # Use a Box instead of Adw.ActionRow to avoid focus errors and hardcoded theme styles
        row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        row_box.set_hexpand(True)
        row_box.add_css_class("playlist-row")
        row_box.set_can_focus(False) # Resolve Gtk-CRITICAL crashes
        list_item.set_child(row_box)

        # Prefix: "Now Playing" indicator
        prefix_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        prefix_box.set_size_request(32, -1)
        prefix_box.set_valign(Gtk.Align.CENTER)
        
        status_icon = Gtk.Image.new_from_icon_name("audio-volume-high-symbolic")
        status_icon.set_visible(False)
        status_icon.add_css_class("now-playing-indicator")
        prefix_box.append(status_icon)
        row_box.append(prefix_box)
        list_item._status_icon = status_icon

        # Content: Title and Subtitle
        content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        content_box.set_valign(Gtk.Align.CENTER)
        content_box.set_hexpand(True)
        row_box.append(content_box)

        title_label = Gtk.Label(xalign=0)
        title_label.add_css_class("title")
        title_label.set_ellipsize(Pango.EllipsizeMode.END)
        content_box.append(title_label)
        list_item._title_label = title_label

        subtitle_label = Gtk.Label(xalign=0)
        subtitle_label.add_css_class("subtitle")
        subtitle_label.set_ellipsize(Pango.EllipsizeMode.END)
        content_box.append(subtitle_label)
        list_item._subtitle_label = subtitle_label

        # Suffix: Duration
        duration_label = Gtk.Label()
        duration_label.add_css_class("caption")
        duration_label.set_valign(Gtk.Align.CENTER)
        row_box.append(duration_label)
        list_item._duration_label = duration_label

        # Gestures
        gesture = Gtk.GestureClick.new()
        row_box.add_controller(gesture)
        list_item._click_gesture = gesture

        gesture_right = Gtk.GestureClick.new()
        gesture_right.set_button(3) # Right click
        row_box.add_controller(gesture_right)
        list_item._right_click_gesture = gesture_right

        # Drag Source for reordering
        drag_source = Gtk.DragSource.new()
        drag_source.set_actions(Gdk.DragAction.MOVE)
        drag_source.connect("prepare", self._on_row_drag_prepare, list_item)
        row_box.add_controller(drag_source)

        # Drop Target for reordering
        drop_target = Gtk.DropTarget.new(Song.__gtype__, Gdk.DragAction.MOVE)
        drop_target.connect("drop", self._on_row_drop, list_item)
        row_box.add_controller(drop_target)



    def _on_playlist_item_bind(self, factory, list_item):
        """Bind song data to the widgets."""
        row_box = list_item.get_child()
        song = list_item.get_item() 

        # Bind is_playing to status_icon visibility
        status_icon = getattr(list_item, "_status_icon", None)
        if hasattr(list_item, "_playing_binding") and list_item._playing_binding:
            list_item._playing_binding.unbind()
            
        list_item._playing_binding = song.bind_property("is_playing", status_icon, "visible", GObject.BindingFlags.SYNC_CREATE)

        # Update row highlight and icon based on playing state
        def on_playing_changed(obj, pspec):
            if obj.is_playing:
                row_box.add_css_class("playing-row")
                
                # Dynamic icon: Refresh if repeating, Volume High if normal
                repeat = self.action_group.get_action_state("repeat").get_boolean()
                icon = "media-playlist-repeat-song-symbolic" if repeat else "audio-volume-high-symbolic"
                status_icon.set_from_icon_name(icon)
            else:
                row_box.remove_css_class("playing-row")
        
        # Connect and set initial state
        song_handler_id = song.connect("notify::is-playing", on_playing_changed)
        on_playing_changed(song, None)
        
        # Store handler to disconnect later
        list_item._song_handler_id = song_handler_id
        list_item._song_obj = song

        # Set labels directly on stored labels
        title_label = getattr(list_item, "_title_label", None)
        subtitle_label = getattr(list_item, "_subtitle_label", None)

        if title_label:
            title_label.set_label(song.title)
        
        # Subtitle: Artist - Album (if available)
        subtitle_parts = []
        if song.artist and song.artist != "Unknown Artist":
            subtitle_parts.append(song.artist)
        if song.album and song.album != "Unknown Album":
            subtitle_parts.append(song.album)
        
        subtitle_text = " - ".join(subtitle_parts) if subtitle_parts else "Unknown Artist"
        if subtitle_label:
            subtitle_label.set_label(subtitle_text)

        full_tooltip = f"{song.title}\n{subtitle_text}"
        row_box.set_tooltip_text(full_tooltip)

        # Duration suffix
        duration_label = getattr(list_item, "_duration_label", None)
        if duration_label:
            if song.duration and song.duration > 0:
                duration_sec = song.duration // Gst.SECOND
                duration_str = f"{duration_sec // 60}:{duration_sec % 60:02d}"
                duration_label.set_label(duration_str)
            else:
                duration_label.set_label("")

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
            
        if hasattr(list_item, "_song_handler_id") and hasattr(list_item, "_song_obj"):
            list_item._song_obj.disconnect(list_item._song_handler_id)
            list_item._song_handler_id = None
            list_item._song_obj = None
            
        row = list_item.get_child()
        if row:
            row.remove_css_class("playing-row")

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
        """Shows context menu for a song row."""
        # Update selection to this row if not already selected
        row_pos = list_item.get_position()
        if row_pos != Gtk.INVALID_LIST_POSITION:
             # Only select if not already part of a multi-selection (if we supported that)
             # SInce single selection, just select it.
             self.selection_model.set_selected(row_pos)

        menu = Gio.Menu()
        menu.append(_("Remove from Playlist"), "win.remove_selected_song")
        
        # New Context Menu Items
        menu.append(_("Repeat this song"), "win.repeat")
        
        if song.uri.startswith("file://"):
             # Pass URI as parameter
             menu.append(_("Show in File Manager"), f"win.show_file_location('{song.uri}')")
        
        # Create a PopoverMenu
        popover = Gtk.PopoverMenu.new_from_model(menu)
        popover.set_has_arrow(False)
        
        # Position it at the cursor
        rect = Gdk.Rectangle()
        rect.x = int(x)
        rect.y = int(y)
        rect.width = 1
        rect.height = 1
        popover.set_pointing_to(rect)
        
        popover.set_parent(list_item.get_child()) 
        popover.popup()
        
    def _on_remove_selected_song_action(self, action, param):
        """Removes the currently selected song from the playlist."""
        selected_pos = self.selection_model.get_selected()
        if selected_pos != Gtk.INVALID_LIST_POSITION:
            self.playlist_store.remove(selected_pos)
            
            # If we removed the playing song, stop playback?
            # Or just let it finish? Usually safer to stop or handle gracefully.
            # For now, let's just let it be, but update song display if it was current
            if not self.current_song and self.playlist_store.get_n_items() == 0:
                self._stop_playback()

    def _on_clear_playlist_action(self, action, param):
        """Clears the entire playlist."""
        self.playlist_store.remove_all()
        self._stop_playback()

    def _stop_playback(self):
        """Stops playback and clears UI."""
        if self.player:
            self.player.set_state(Gst.State.NULL)
            self.play_pause_button.set_icon_name(self.PLAY_ICON)
        self.current_song = None
        self._media_duration = 0
        self._update_song_display(None)
        
    def _on_song_row_activated(self, gesture, n_press, x, y, song):
        """Called when a song row is clicked (left click)."""
        # Single click selects
        # Double click plays
        
        current_selection = self.selection_model.get_selected()
        clicked_pos = -1
        
        # Find position of this song
        n = self.playlist_store.get_n_items()
        for i in range(n):
            if self.playlist_store.get_item(i) == song:
                clicked_pos = i
                break
        
        if clicked_pos != -1:
             # Always update selection on click
             if clicked_pos != current_selection:
                 self.selection_model.set_selected(clicked_pos)
             
             # Only play purely on double click
             if n_press == 2:
                 print(f"Double click on row: {song.title}. Playing.")
                 self.play_uri(song.uri)

    def _on_playlist_selection_changed(self, selection_model, position, n_items):
        """Handle selection change."""
        # Note: We NO LONGER auto-play on selection change.
        # Playback is triggered by:
        # 1. Double click (mamo/ui/window.py:_on_song_row_activated)
        # 2. Explicit play actions (Next, Prev, Open Playlist, etc.)
        pass
                
    def _on_playlist_key_pressed(self, controller, keyval, keycode, state):
        """Handle keys: Delete to remove, Enter to play."""
        if keyval == Gdk.KEY_Delete:
             self.action_group.activate_action("remove_selected_song", None)
             return True
        elif keyval == Gdk.KEY_Return or keyval == Gdk.KEY_KP_Enter:
             selected_pos = self.selection_model.get_selected()
             if selected_pos != Gtk.INVALID_LIST_POSITION:
                 song = self.playlist_store.get_item(selected_pos)
                 if song and song.uri:
                     self.play_uri(song.uri)
                 return True
        return False

    def _on_play_album_clicked(self, button):
        """Opens the Album Browser dialog."""
        browser = AlbumBrowser(self, self.library_manager, self._on_album_browser_selection)
        browser.present()

    def _on_album_browser_selection(self, command, data):
        """Callback from Album Browser."""
        if command == "play":
            album = data
            songs = self.library_manager.get_album_songs(album)
            if songs:
                # Replace playlist
                self.playlist_store.remove_all()
                for s in songs:
                    self.playlist_store.append(s)
                
                self.selection_model.set_selected(0)
                # Playback starts via selection-changed but if 0 was already selected (e.g. from previous playlist of same size?), 
                # or if playing stopped, selection-changed might not fire or might think nothing changed.
                # Explicitly play the first song to be sure.
                if len(songs) > 0:
                    self.play_uri(songs[0].uri)

        elif command == "play_all_albums":
             # Play all albums (add all known songs?)
             # Assuming 'data' is irrelevant or None
             # This could be implemented to shuffle all songs.
             all_songs = self.library_manager.get_all_songs()
             random.shuffle(all_songs) # Basic shuffle
             
             self.playlist_store.remove_all()
             for s in all_songs:
                 self.playlist_store.append(s)
             self.selection_model.set_selected(0)
             if len(all_songs) > 0:
                  self.play_uri(all_songs[0].uri)
        
        elif command == "queue":
            album = data
            songs = self.library_manager.get_album_songs(album)
            if songs:
                for s in songs:
                    self.playlist_store.append(s)
            
            # If playlist was empty, this might trigger something?
            # If nothing playing, maybe start?
            if not self.current_song and self.playlist_store.get_n_items() > 0:
                 self.selection_model.set_selected(0)


    def _on_add_clicked(self, button):
        """Trigger 'add folder' action."""
        self.action_group.activate_action("add_folder_new", None)

    def _on_add_file_clicked(self, button):
        """Trigger 'add file' action."""
        self.action_group.activate_action("add_file", None)

    def _show_file_dialog(self): 
        """Show file chooser dialog for adding files."""
        dialog = Gtk.FileDialog.new()
        dialog.set_title(_("Select Audio File(s)"))
        dialog.set_modal(True)
        
        filter_audio = Gtk.FileFilter.new()
        filter_audio.set_name("Audio Files")
        filter_audio.add_mime_type("audio/*")
        
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(filter_audio)
        dialog.set_filters(filters)
        dialog.set_default_filter(filter_audio)

        dialog.open_multiple(parent=self, cancellable=None, callback=self._on_open_multiple_finish)

    def _on_open_multiple_finish(self, dialog, result):
        """Callback for file chooser."""
        try:
             files = dialog.open_multiple_finish(result)
             for i in range(files.get_n_items()):
                 f = files.get_item(i)
                 self._discover_and_add_uri(f.get_uri())
        except GLib.Error as e:
            if not e.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED):
                print(f"Error opening files: {e.message}", file=sys.stderr)

    def _discover_and_add_uri(self, uri):
        """Starts discovery for a URI to add it to the playlist."""
        if not uri: return
        self.discoverer.discover_uri_async(uri)

    def _start_folder_scan(self, folder_file):
        """Recursively scans a folder for audio files and adds them."""
        # Run in a thread to avoid blocking UI
        thread = threading.Thread(target=self._folder_scan_thread, args=(folder_file,), daemon=True)
        thread.start()

    def _folder_scan_thread(self, folder_file):
        """Thread function for folder scanning."""
        root_path = folder_file.get_path()
        if not root_path: return
        
        # We can reuse LibraryManager's logic if we want, or just simple walk
        # Let's do simple walk for "Add Folder" to playlist (distinct from Library Library)
        # Actually, user might want them to be in Library too?
        # For now, just add to playlist.
        
        audio_exts = {'.mp3', '.flac', '.ogg', '.m4a', '.wav', '.wma', '.opus'}
        
        # Collect all URIs first to batch add? Or add as we go?
        # Adding as we go is better for feedback but might thrash UI.
        # Let's collect a chunk.
        
        found_uris = []
        try:
            for root, dirs, files in os.walk(root_path):
                for f in files:
                    ext = os.path.splitext(f)[1].lower()
                    if ext in audio_exts:
                        full_path = os.path.join(root, f)
                        found_uris.append(pathlib.Path(full_path).as_uri())
        except Exception as e:
            print(f"Error scanning folder {root_path}: {e}")

        # Now discover them
        # Note: discoverer is async. We can just fire them all off.
        # But thousands might choke it.
        # Let's schedule them on main thread.
        GLib.idle_add(self._schedule_discoveries, found_uris)

    def _schedule_discoveries(self, uris):
        """Schedules discovery for a list of URIs."""
        for uri in uris:
            self.discoverer.discover_uri_async(uri)
        return False

    def _on_discoverer_finished(self, discoverer):
        """Called when discoverer has finished (all processing?). No, for each?"""
        # Verify signal signature.
        pass

    def _on_discoverer_discovered(self, discoverer, info, error):
        """Called when a URI has been discovered."""
        if error:
            print(f"Discovery error for {info.get_uri()}: {error.message}", file=sys.stderr)
            return

        uri = info.get_uri()
        duration_ns = info.get_duration()
        tags = info.get_tags()

        title = "Unknown Title"
        artist = "Unknown Artist"
        album = "Unknown Album"
        
        # Initial guess from filename
        # ... (simplified)
        
        if tags:
             # GstTagList
             # We need to extract strings.
             # Helper to get string:
             def get_tag_str(tag_name):
                 res, val = tags.get_string(tag_name)
                 return val if res else None

             t = get_tag_str(Gst.TAG_TITLE)
             if t: title = t
             
             a = get_tag_str(Gst.TAG_ARTIST)
             if a: artist = a
             
             alb = get_tag_str(Gst.TAG_ALBUM)
             if alb: album = alb
             
        # Fallback to filename if "Unknown"
        if title == "Unknown Title":
            p = unquote(urlparse(uri).path)
            title = os.path.splitext(os.path.basename(p))[0]

        # Extract Album Art?
        # Discoverer might provide it via tags 'image' or 'preview-image'
        # But usually we need to extract from file manually or via existing cache.
        # We'll let the Song object handle it or do it lazily.
        # Or checking external files.
        # For drag/drop files, we don't have them in LibraryManager cache necessarily.
        
        # Check external art cache (populated by _scan_for_external_art if we did that)
        # We haven't implemented comprehensive art scan for added files here.
        # Let's try basic extraction if we have time, but better to just add item.
        # Models.Song handles lazy loading? No, it expects bytes in constructor or property.
        
        # Try to find art in the directory?
        # Try to find art in the directory?
        path = self._uri_to_path(uri)
        art_bytes = None
        if path:
             # Check if we have cached art for this folder
             folder = os.path.dirname(path)
             if folder in self._external_art_cache:
                 art_bytes = self._external_art_cache[folder]
             else:
                 # Try to find cover.jpg etc
                 found_path = self._find_cover_in_folder(folder)
                 if found_path:
                     try:
                         # Load into GLib.Bytes
                         with open(found_path, "rb") as f:
                             data = f.read()
                             art_bytes = GLib.Bytes.new(data)
                             self._external_art_cache[folder] = art_bytes
                     except Exception as e:
                         print(f"Error loading external artwork {found_path}: {e}")

        # If no folder art found, try embedded art
        if not art_bytes:
             art_bytes = LibraryManager.detect_embedded_art(path)

        song = Song(uri=uri, title=title, artist=artist, album=album, duration=duration_ns)
        if art_bytes:
            song.album_art_data = art_bytes
        
        # Add to playlist
        self.playlist_store.append(song)
        
        # Trigger waveform analysis
        self._start_waveform_analysis(song)
        
        # If this was the first song and auto-play is on
        if self._auto_play_after_load and self.playlist_store.get_n_items() == 1:
            self.selection_model.set_selected(0)
            self.play_uri(song.uri)
            self._auto_play_after_load = False

    def _on_player_message(self, bus, message):
        """Handles messages from the GStreamer bus."""
        t = message.type
        if t == Gst.MessageType.ERROR:
            err, dbg = message.parse_error()
            print(f"ERROR: {err.message} ({dbg})", file=sys.stderr)
            
            # Show toast for error
            toast = Adw.Toast.new(f"Playback Error: {err.message}")
            toast.set_timeout(3)
            self.toast_overlay.add_toast(toast)
            
            if self.player:
                self.player.set_state(Gst.State.NULL)
                self.play_pause_button.set_icon_name(self.PLAY_ICON)
                self.waveform.set_fraction(0.0)
                self.waveform.set_sensitive(False)
                self.current_song = None
                self._update_song_display(None)
        
        elif t == Gst.MessageType.EOS:
            print("End of stream reached.")
            
            # Auto-advance logic
            # Check repeat mode
            repeat = self.action_group.get_action_state("repeat").get_boolean()
            if repeat:
                print("Repeat is ON. Restarting track.")
                # Seek to 0
                self.player.seek_simple(Gst.Format.TIME, Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT, 0)
            else:
                self._on_next_clicked(None)

        elif t == Gst.MessageType.STATE_CHANGED:
            old_state, new_state, pending_state = message.parse_state_changed()
            if message.src == self.player:
                # print(f"State changed: {old_state} -> {new_state}")
                if new_state == Gst.State.PLAYING:
                    self.play_pause_button.set_icon_name(self.PAUSE_ICON)
                    if not self._progress_timer_id:
                        self._progress_timer_id = GLib.timeout_add(100, self._update_progress)
                    self.waveform.set_sensitive(True)
                elif new_state == Gst.State.PAUSED:
                    self.play_pause_button.set_icon_name(self.PLAY_ICON)
                elif new_state == Gst.State.NULL:
                    self.play_pause_button.set_icon_name(self.PLAY_ICON)
                    self.waveform.set_fraction(0.0)
                    self.waveform.set_sensitive(False)
                    self.time_label_current.set_label("0:00")
                    self.time_label_remaining.set_label("-0:00")
                
                self.mpris.update_playback_status()

        elif t == Gst.MessageType.ELEMENT:
            struct = message.get_structure()
            # print(f"Element message: {struct.get_name()}")
            if struct.get_name() == "level":
                pass
                
    def _update_song_display(self, song):
        """Updates the UI with the given song's metadata."""
        
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
            
            # Update window title for taskbar/WM
            display_artist = song.artist or "Unknown Artist"
            self.set_title(f"{display_artist} - {song.title}")

            
            duration_ns = song.duration
            if duration_ns is not None and duration_ns != Gst.CLOCK_TIME_NONE and duration_ns > 0:
                dur_sec = duration_ns // Gst.SECOND
                duration_str = f"{dur_sec // 60}:{dur_sec % 60:02d}"
            else:
                duration_str = "--:--"
            self.time_label_current.set_label("0:00")
            self.time_label_remaining.set_label("--:--")

            
            glib_bytes_data = song.album_art_data
            pixbuf = None
            
            if glib_bytes_data:
                raw_bytes_data = glib_bytes_data.get_data() 
                try:
                    loader = GdkPixbuf.PixbufLoader()
                    loader.write(raw_bytes_data)
                    loader.close()
                    pixbuf = loader.get_pixbuf()
                    
                    scaled_pixbuf = pixbuf.scale_simple(320, 320, GdkPixbuf.InterpType.HYPER)
                    self.cover_image.set_from_pixbuf(scaled_pixbuf)
                    
                except Exception as e:
                    print(f"Error loading album art in _update_song_display for '{song.title}': {e}", file=sys.stderr)
                    # Already set to generic icon above
            else:
                self.cover_image.set_from_icon_name("audio-x-generic-symbolic")
            
            if song.waveform_data:
                self.waveform.set_waveform_data(song.waveform_data)
            else:
                self.waveform.set_waveform_data([])
            
            # Apply dynamic tint if enabled
            if self.action_group.get_action_state("album_tinting").get_boolean() and glib_bytes_data:
                try:
                    # Reuse pixbuf from above for efficiency
                    self._update_dynamic_tint(pixbuf)
                except Exception as tint_e:
                    print(f"Error applying tint: {tint_e}", file=sys.stderr)
                    self._clear_dynamic_tint()
            else:
                self._clear_dynamic_tint()
            
        else:
            self.set_title("Mamo")
            self._clear_dynamic_tint()
            
            
            self.song_label.set_label("")
            self.song_label.set_tooltip_text("") 
            self.artist_label.set_label("")
            self.time_label_current.set_label("")
            self.time_label_remaining.set_label("")
            self.waveform.set_waveform_data([])
            self._clear_dynamic_tint()

    def _update_dynamic_tint(self, pixbuf):
        """Extracts average color from pixbuf and applies it as a background tint."""
        if not pixbuf:
            self._clear_dynamic_tint()
            return

        # Scale to 1x1 to get average RGB
        small = pixbuf.scale_simple(1, 1, GdkPixbuf.InterpType.BILINEAR)
        pixels = small.get_pixels()
        if not pixels or len(pixels) < 3:
            self._clear_dynamic_tint()
            return
            
        r, g, b = pixels[0], pixels[1], pixels[2]
        
        # Apply subtle tint (0.15 alpha for active, 0.07 for backdrop)
        # Use CSS transitions for smoothness, synchronized between both classes
        css = """
        .main-toolbar-view, .main-toolbar-view:backdrop {
            transition: background-color 0.5s ease-in-out;
        }
        .main-toolbar-view {
            background-color: rgba(%d, %d, %d, 0.4);
        }
        .main-toolbar-view:backdrop {
            background-color: rgba(%d, %d, %d, 0.2);
        }
        .main-player-box, .main-header-bar,
        .main-player-box:backdrop, .main-header-bar:backdrop {
            background-color: transparent;
            background-image: none;
            box-shadow: none;
        }
        .main-player-box {
            padding: 12px;
            border-bottom: 1px solid rgba(0, 0, 0, 0.1);
        }
        """ % (r, g, b, r, g, b)
        self._dynamic_tint_provider.load_from_data(css.encode('utf-8'))
        
        # Calculate brighter color for waveform
        # Simple blend with white
        w_factor = 0.6
        wr = r + (255 - r) * w_factor
        wg = g + (255 - g) * w_factor
        wb = b + (255 - b) * w_factor
        
        if hasattr(self, 'waveform'):
            self.waveform.set_active_color((wr/255.0, wg/255.0, wb/255.0, 1.0))

    def _clear_dynamic_tint(self):
        """Clears the dynamic background tint."""
        css = """
        .main-toolbar-view, .main-toolbar-view:backdrop {
            background-color: transparent;
            transition: background-color 0.5s ease-in-out;
        }
        .main-player-box, .main-header-bar,
        .main-player-box:backdrop, .main-header-bar:backdrop {
            background-color: transparent;
            background-image: none;
            box-shadow: none;
        }
        .main-player-box {
            padding: 12px;
            border-bottom: none;
        }
        """
        self._dynamic_tint_provider.load_from_data(css.encode('utf-8'))
        
        if hasattr(self, 'waveform'):
             self.waveform.set_active_color(None)

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
        dialog.set_title(_("Open Playlist"))
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
        dialog.set_title(_("Save Playlist As"))
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
        dialog.set_title(_("Select Folder(s) to Add"))
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
        about_window.set_application_icon("org.broomlabs.MamoMusicPlayer")
        about_window.set_version("0.0.1") 
        about_window.set_developer_name("") 
        about_window.set_copyright("© 2026 David Philip Barr,  2025 Robert Renling for Namo") 
        about_window.set_developers([ "David Barr", "Robert Renling", "Nod to Pete Johanson", "Hat tip to Jorn Baayen."]) 
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

                    album_tinting = settings.get("album_tinting", True)
                    at_action = self.action_group.lookup_action("album_tinting")
                    if at_action:
                        at_action.change_state(GLib.Variant.new_boolean(album_tinting))

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
            "album_tinting": self.action_group.get_action_state("album_tinting").get_boolean(),
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
        
        # Trigger update of the now playing icon
        if self.current_song:
            self.current_song.notify("is-playing")

    def _on_loop_all_action_activated(self, action, parameter):
        """Toggles loop all setting."""
        state = action.get_state().get_boolean()
        new_state = not state
        action.change_state(GLib.Variant.new_boolean(new_state))
        print(f"Loop All toggled to: {new_state}")
        self._save_settings()
        self._update_playback_controls_sensitivity()

    def _on_album_tinting_action_activated(self, action, parameter):
        """Toggles album tinting and updates the UI."""
        state = action.get_state().get_boolean()
        new_state = not state
        action.change_state(GLib.Variant.new_boolean(new_state))
        print(f"Album Tinting toggled to: {new_state}")
        self._update_song_display(self.current_song)

    def _on_show_file_location(self, action, parameter):
        """Opens the file manager at the song's location."""
        uri = parameter.get_string()
        if not uri: return

        print(f"Show location for: {uri}")
        try:
             # Parse URI to get path
             p = urlparse(uri)
             path = unquote(p.path)
             if not os.path.exists(path):
                 print(f"Path does not exist: {path}")
                 return
             
             folder = os.path.dirname(path)
             if os.path.isdir(folder):
                 subprocess.Popen(["xdg-open", folder])
        except Exception as e:
            print(f"Error opening file location: {e}")
        self._save_settings()
        
        # Trigger update immediately
        self._update_song_display(self.current_song)

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

    def _find_cover_in_folder(self, folder_path):
        """Scans for common cover art filenames in the folder."""
        common_names = ["cover.jpg", "Cover.jpg", "folder.jpg", "Folder.jpg", 
                        "artwork.jpg", "Artwork.jpg", "front.jpg", "Front.jpg"]
        
        # also check png
        common_names.extend([n.replace(".jpg", ".png") for n in common_names])
        
        for name in common_names:
            p = os.path.join(folder_path, name)
            if os.path.exists(p):
                return p
        
        # Scan if not found?
        try:
            for f in os.listdir(folder_path):
                lower = f.lower()
                if "cover" in lower or "front" in lower or "folder" in lower:
                    if lower.endswith(".jpg") or lower.endswith(".png") or lower.endswith(".jpeg"):
                        return os.path.join(folder_path, f)
        except:
            pass
            
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





