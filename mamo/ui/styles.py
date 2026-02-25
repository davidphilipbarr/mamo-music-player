
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

.main-playlist-card {
    border-radius: 12px;
    overflow: hidden;
    background-color: alpha(@card_bg_color, 0.4);
    border: 1px solid alpha(currentColor, 0.1);
}

/* Force clipping on all layers */
.playlist-view, 
.playlist-view-scrolled,
.playlist-view-scrolled listview {
    border-radius: 12px;
    overflow: hidden;
    background-color: transparent;
    --accent-bg-color: alpha(currentColor, 0.15);
    --accent-fg-color: inherit;
}

/* Aggressive Selection Override */
.playlist-view listitem:selected,
.playlist-view listitem:selected:hover,
.playlist-view listitem:selected:focus,
.playlist-view :selected {
    background-color: alpha(currentColor, 0.15) !important;
    background-image: none !important;
    box-shadow: none !important;
    border: none !important;
    outline: none !important;
    color: inherit !important;
}

/* Ensure all children of selected items inherit the transparency and color */
.playlist-view listitem:selected > *,
.playlist-view listitem:selected label,
.playlist-view :selected > * {
    background-color: transparent !important;
    background-image: none !important;
    color: inherit !important;
}

.playlist-view .playing-row {
    background: alpha(currentColor, 0.1) !important;
    border-radius: 8px;
}

.playlist-view .playing-row:selected {
    background: alpha(currentColor, 0.25) !important;
}

/* Custom row layout styling */
.playlist-row, .album-row {
    padding: 8px 12px;
}

.playlist-row .title, .album-row .title-4 {
    font-size: 11pt;
    font-weight: bold;
}

.playlist-row .subtitle, .album-row .caption {
    font-size: 10pt;
    opacity: 0.8;
}

.playlist-row .caption {
    font-size: 9pt;
    opacity: 0.7;
}

.playlist-view-scrolled undershoot.top {
    border-top-left-radius: 12px;
    border-top-right-radius: 12px;
}

.playlist-view-scrolled undershoot.bottom {
    border-bottom-left-radius: 12px;
    border-bottom-right-radius: 12px;
}
"""
