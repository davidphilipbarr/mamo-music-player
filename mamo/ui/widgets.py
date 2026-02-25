
import gi
from gi.repository import Gtk, Gdk, GObject

class WaveformBar(Gtk.DrawingArea):
    """
    A custom widget that renders a pseudo-waveform using vertical bars.
    Supports seeking via click/drag.
    """
    def __init__(self, seek_callback=None):
        super().__init__()
        self.set_content_width(280)
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

        self.active_color = (0.208, 0.518, 0.894, 1.0) # Adwaita Blue default

    def set_active_color(self, rgba):
        """Sets the active color of the waveform bars (tuple of r,g,b,a). Pass None to reset."""
        if rgba:
            self.active_color = rgba
        else:
            self.active_color = (0.208, 0.518, 0.894, 1.0)
        self.queue_draw()

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
        cr.set_source_rgba(*self.active_color)
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
