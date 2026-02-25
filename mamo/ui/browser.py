
import gi
from gi.repository import Gtk, Adw, Gio, GdkPixbuf, Pango

from ..models import Album

class AlbumBrowser(Adw.Window):
    def __init__(self, parent, library_manager, callback):
        super().__init__(transient_for=parent, modal=True)
        self.set_title(_("Album Browser"))
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
        self.search_entry.set_placeholder_text(_("Search Artist or Album..."))
        self.search_entry.connect("search-changed", self._on_search_changed)
        main_box.append(self.search_entry)

        lib_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        main_box.append(lib_box)
        
        self.lib_label = Gtk.Label(label=_("Library: ") + f"{self.library_manager.library_path}", xalign=0)
        self.lib_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self.lib_label.set_hexpand(True)
        lib_box.append(self.lib_label)

        change_lib_btn = Gtk.Button(label=_("Change..."))
        change_lib_btn.connect("clicked", self._on_change_library_clicked)
        lib_box.append(change_lib_btn)

        re_scan_btn = Gtk.Button(label=_("Rescan"))
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

        cancel_button = Gtk.Button(label=_("Cancel"))
        cancel_button.connect("clicked", lambda x: self.close())
        button_box.append(cancel_button)

        self.queue_button = Gtk.Button(label=_("Queue Album"))
        self.queue_button.connect("clicked", self._on_action_clicked, "queue")
        self.queue_button.set_sensitive(False)
        button_box.append(self.queue_button)

        self.play_button = Gtk.Button(label=_("Add Album"))
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
        box.add_css_class("album-row")
        box.set_can_focus(False) # Resolve Gtk-CRITICAL crashes
        
        image = Gtk.Image()
        image.set_pixel_size(48)
        image.set_from_icon_name("audio-x-generic-symbolic")
        box.append(image)
        list_item._image = image

        details = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        details.set_valign(Gtk.Align.CENTER)
        box.append(details)

        title_label = Gtk.Label(xalign=0)
        title_label.add_css_class("title-4")
        details.append(title_label)
        list_item._title_label = title_label

        artist_label = Gtk.Label(xalign=0)
        artist_label.add_css_class("caption")
        details.append(artist_label)
        list_item._artist_label = artist_label

        list_item.set_child(box)

    def _on_item_bind(self, factory, list_item):
        album = list_item.get_item()
        image = getattr(list_item, "_image", None)
        title_label = getattr(list_item, "_title_label", None)
        artist_label = getattr(list_item, "_artist_label", None)

        if title_label:
            title_label.set_label(album.title)
        if artist_label:
            artist_label.set_label(album.artist)

        if image:
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
            
            self.callback(action, album)
            self.close()

    def _on_change_library_clicked(self, button):
        dialog = Gtk.FileDialog.new()
        dialog.set_title(_("Select Music Library Folder"))
        dialog.select_folder(parent=self, cancellable=None, callback=self._on_library_folder_selected)

    def _on_library_folder_selected(self, dialog, result):
        try:
            folder = dialog.select_folder_finish(result)
            if folder:
                new_path = folder.get_path()
                self.library_manager.library_path = new_path
                self.lib_label.set_label(_("Library: ") + f"{new_path}")
                
                # Tell parent to save new path
                parent = self.get_transient_for()
                if hasattr(parent, 'library_path'):
                    parent.library_path = new_path
                    parent._save_settings()
                
                self.library_manager.start_scan()
        except Exception as e:
            print(f"Error selecting library folder: {e}")
