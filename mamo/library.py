
import gi
import threading
import os
import json
import base64
import mutagen
from gi.repository import GObject, GLib

from .models import Album, Song
import pathlib

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
                            # Try multiple files in the folder for embedded art
                            for f in audio_files[:5]: # Check up to 5 files
                                art_data = self._find_embedded_art(os.path.join(root, f))
                                if art_data:
                                    break
                                    
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
        cover_filenames = ["cover.jpg", "Cover.jpg", "folder.jpg", "Folder.jpg", "cover.png", "Cover.png", "album.jpg", "Album.jpg", "album.png", "Album.png"]
        for fn in cover_filenames:
            path = os.path.join(folder, fn)
            if os.path.exists(path):
                try:
                    with open(path, "rb") as f:
                        return GLib.Bytes.new(f.read())
                except:
                    pass
        return None

    @staticmethod
    def detect_embedded_art(filepath):
        """Extracts embedded album art from an audio file."""
        try:
            audio_raw = mutagen.File(filepath)
            if not audio_raw or not audio_raw.tags:
                return None
            
            art_bytes = None
            if isinstance(audio_raw.tags, mutagen.id3.ID3):
                # Search for any APIC frame
                for tag_name in audio_raw.tags.keys():
                    if tag_name.startswith('APIC'):
                        art_bytes = audio_raw.tags[tag_name].data
                        break
            elif isinstance(audio_raw, mutagen.mp4.MP4) and 'covr' in audio_raw.tags and audio_raw.tags['covr']:
                art_bytes = bytes(audio_raw.tags['covr'][0])
            elif hasattr(audio_raw, 'pictures') and audio_raw.pictures:
                art_bytes = audio_raw.pictures[0].data
            elif hasattr(audio_raw.tags, 'get'):
                # Try common Ogg/Vorbis approach
                pics = audio_raw.tags.get('metadata_block_picture', [])
                if pics:
                    from mutagen.flac import Picture
                    try:
                        p = Picture(base64.b64decode(pics[0]))
                        art_bytes = p.data
                    except:
                        pass
            
            if art_bytes:
                return GLib.Bytes.new(art_bytes)
        except Exception as e:
            print(f"LibraryManager: Error extracting embedded art from {filepath}: {e}")
        return None

    def _find_embedded_art(self, filepath):
        return LibraryManager.detect_embedded_art(filepath)
    def get_album_songs(self, album):
        """Returns a list of Song objects for the given album."""
        songs = []
        if not album.folder or not os.path.exists(album.folder):
            return songs
            
        try:
            for f in os.listdir(album.folder):
                if f.startswith('.'): continue
                if not f.lower().endswith(('.mp3', '.flac', '.m4a', '.ogg', '.opus', '.wav')): continue
                
                full_path = os.path.join(album.folder, f)
                uri = pathlib.Path(full_path).as_uri()
                
                # Extract metadata
                title = None
                artist = None
                album_title = None
                duration_ns = 0
                track_num = 0
                
                try:
                    audio = mutagen.File(full_path, easy=True)
                    if audio:
                        title = audio.get('title', [None])[0]
                        artist = audio.get('artist', [None])[0]
                        album_title = audio.get('album', [None])[0]
                        
                        track_num_str = audio.get('tracknumber', ['0'])[0]
                        try:
                            # Handle "1/12" format
                            if '/' in track_num_str:
                                track_num = int(track_num_str.split('/')[0])
                            else:
                                track_num = int(track_num_str)
                        except:
                            track_num = 0

                        if audio.info and hasattr(audio.info, 'length'):
                             duration_ns = int(audio.info.length * 1000000000)
                except Exception as e:
                    print(f"LibraryManager: Error reading tags from {full_path}: {e}")

                # Fallback to filename if title missing
                if not title:
                    title = os.path.splitext(f)[0]
                
                # If artist/album missing, use album object's data as fallback
                if not artist: artist = album.artist
                if not album_title: album_title = album.title
                
                song = Song(uri=uri, title=title, artist=artist, album=album_title, duration=duration_ns)
                song.album_art_data = album.art_data # Propagate album art
                song._track_num = track_num # Store for sorting
                
                songs.append(song)
            
            # Sort by track number, then title
            songs.sort(key=lambda s: (getattr(s, '_track_num', 0), s.title))
            
        except Exception as e:
            print(f"Error listing songs for album {album.title}: {e}")
            
        return songs

    def get_all_songs(self):
        """Returns a list of all songs in the library (expensive)."""
        all_songs = []
        for album in self.albums:
            all_songs.extend(self.get_album_songs(album))
        return all_songs
