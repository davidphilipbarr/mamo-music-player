#!/usr/bin/env python3
import sys
import os
import gettext
import gi

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
gi.require_version('Gst', '1.0')

from gi.repository import Gtk, Adw, Gst, Gio, GLib, Gdk


from mamo.ui.window import MamoWindow
from mamo.ui.styles import STYLE_CSS

# Setup translation
localedir = os.path.join(os.path.dirname(__file__), "locale")
gettext.bindtextdomain("mamo", localedir)
gettext.textdomain("mamo")
gettext.install("mamo", localedir)

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
            # Check user preference
            auto_play = True
            if self.window.action_group:
                state = self.window.action_group.get_action_state("auto_play")
                if state:
                    auto_play = state.get_boolean()
            
            if auto_play:
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
            Gtk.STYLE_PROVIDER_PRIORITY_USER
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
