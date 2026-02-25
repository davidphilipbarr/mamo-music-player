
import gi
import os
import tempfile
import pathlib
from gi.repository import Gio, GLib, Gst

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
                GLib.idle_add(lambda: (self.window._on_next_clicked(), False)[1])
                invocation.return_value(None)
            elif method_name == "Previous":
                GLib.idle_add(lambda: (self.window._on_prev_clicked(None), False)[1])
                invocation.return_value(None)
            elif method_name == "Pause":
                GLib.idle_add(lambda: (self.window.player.set_state(Gst.State.PAUSED), False)[1])
                invocation.return_value(None)
            elif method_name == "PlayPause":
                GLib.idle_add(lambda: (self.window.toggle_play_pause(), False)[1])
                invocation.return_value(None)
            elif method_name == "Stop":
                GLib.idle_add(lambda: (self.window.player.set_state(Gst.State.NULL), False)[1])
                invocation.return_value(None)
            elif method_name == "Play":
                GLib.idle_add(lambda: (self.window.player.set_state(Gst.State.PLAYING), False)[1])
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
