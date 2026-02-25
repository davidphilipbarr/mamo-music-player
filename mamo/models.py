
import gi
from gi.repository import GObject, GLib

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
