"""Design tokens: the single source of truth for colors and radii.

Every color used by the Qt stylesheets (``_styles.py``) and the application
palettes lives here.  ``_styles.py`` renders its single QSS template twice --
once with :data:`DARK_TOKENS` and once with :data:`LIGHT_TOKENS``.

Keys are semantic; values are CSS/QSS color strings (hex or ``rgba()``).
Radius tokens carry their unit (``px``) so they can be dropped into QSS
declarations directly.

The type scale below is the single source for font sizes: every QSS
``font-size`` rule and the application font use these six levels, so the
whole UI shares one px-based ladder (no pt mixing).
"""

# -- Type scale (px; identical for both themes) --
FONT_XS = 14  # auxiliary captions, timestamps, status footnotes
FONT_SM = 15  # secondary text, field labels, badges
FONT_BASE = 16  # body text: buttons, inputs, list items, plain labels
FONT_MD = 18  # card/group titles, emphasized values
FONT_LG = 22  # page and dialog titles
FONT_METRIC = 40  # dashboard stat figures only

FONT_SCALE_TOKENS = {
    "font_xs": f"{FONT_XS}px",
    "font_sm": f"{FONT_SM}px",
    "font_base": f"{FONT_BASE}px",
    "font_md": f"{FONT_MD}px",
    "font_lg": f"{FONT_LG}px",
    "font_metric": f"{FONT_METRIC}px",
}

# ---------------------------------------------------------------------------
# Dark theme
# ---------------------------------------------------------------------------

DARK_TOKENS = {
    **FONT_SCALE_TOKENS,
    # -- Radii / spacing --
    "radius_panel": "16px",
    "radius_inner": "12px",
    "radius_control": "8px",
    "panel_padding": "16px",

    # -- Surfaces --
    "bg_base": "#1A1D27",
    "bg_surface": "rgba(26, 31, 44, 0.95)",
    "bg_inner": "rgba(15, 17, 25, 0.6)",
    "bg_inner_readonly": "rgba(15, 17, 25, 0.35)",
    "bg_popup": "#252836",
    "bg_pane": "rgba(28, 31, 42, 0.95)",
    "bg_panel": "rgba(255, 255, 255, 0.03)",
    "bg_card": "rgba(255, 255, 255, 0.04)",
    "bg_preview": "rgba(15, 17, 25, 0.4)",
    "bg_table": "rgba(15, 17, 25, 0.5)",
    "bg_table_alt": "rgba(255, 255, 255, 0.02)",
    "bg_header": "rgba(35, 38, 52, 0.95)",
    "bg_statusbar": "rgba(17, 19, 27, 0.95)",
    "bg_tooltip": "#2A2E3C",
    "bg_messagebox": "#1E2130",
    "bg_font_preview": "rgba(15, 17, 25, 0.34)",
    "bg_log_panel": "#14161F",
    "bg_log_text": "#0E1018",
    "sidebar_bg": "#1B1E2A",
    "dialog_bg": "#1F2332",

    # -- Borders / lines --
    "border": "rgba(255, 255, 255, 0.08)",
    "border_subtle": "rgba(255, 255, 255, 0.06)",
    "border_panel": "rgba(255, 255, 255, 0.06)",
    "nav_item_border": "rgba(255, 255, 255, 0.14)",
    "border_control": "rgba(255, 255, 255, 0.08)",
    "border_popup": "rgba(255, 255, 255, 0.1)",
    "border_pane": "rgba(255, 255, 255, 0.05)",
    "border_dialog": "rgba(255, 255, 255, 0.12)",
    "border_dashed": "rgba(255, 255, 255, 0.12)",
    "gridline": "rgba(255, 255, 255, 0.04)",
    "table_row_border": "rgba(255, 255, 255, 0.03)",

    # -- Text --
    "text_primary": "#FFFFFF",
    "text_bright": "#E0E6F0",
    "text_secondary": "#C0C8E0",
    "text_contrast": "#C0C8E0",
    "text_muted": "#A0AACC",
    "text_soft": "#A0AACC",
    "text_faded": "#8890A8",
    "text_dim": "#8890A8",
    "text_pill": "#8890A8",
    "text_tab": "#6B7394",
    "text_tab_hover": "#A0AACC",
    "text_brand": "#505672",
    "text_disabled": "#5A6380",
    "text_readonly": "#9AA0B8",
    "text_subtitle": "#8A93B0",
    "text_log_status": "#6E7488",
    "text_on_accent": "#FFFFFF",

    # -- Accent (single hue; used sparingly for primary actions,
    #    selection and focus) --
    "accent": "#5B7CF7",
    "accent_hover": "#6E8EFF",
    "accent_pressed": "#4A6AE0",
    # Accent variant used by checkboxes, sliders, tabs, progress bars.
    "control_accent": "#5B7CF7",
    "accent_focus": "rgba(91, 124, 247, 0.6)",
    "indicator_hover_border": "rgba(91, 124, 247, 0.5)",

    # -- Selection --
    "selection_bg": "rgba(91, 124, 247, 0.4)",
    "selection_bg_popup": "rgba(91, 124, 247, 0.3)",
    "table_selection_bg": "rgba(91, 124, 247, 0.2)",
    "table_item_selected": "rgba(91, 124, 247, 0.18)",
    "tab_selected_bg": "rgba(91, 124, 247, 0.08)",
    "tab_hover_border": "rgba(91, 124, 247, 0.25)",
    "selection_log": "#2C3142",

    # -- Status --
    "success": "#6DDC8C",
    "success_bg": "rgba(109, 220, 140, 0.08)",
    "success_border": "rgba(109, 220, 140, 0.35)",
    "warning": "#F3C76A",
    "danger": "#F06470",
    "danger_bg": "rgba(240, 100, 112, 0.12)",
    "status_on": "#6DDC8C",
    "status_off": "#F06470",

    # -- Controls --
    "btn_disabled_bg": "rgba(255, 255, 255, 0.06)",
    "tool_bg": "rgba(255, 255, 255, 0.06)",
    "tool_bg_hover": "rgba(255, 255, 255, 0.12)",
    "tool_border_hover": "rgba(255, 255, 255, 0.15)",
    "indicator_bg": "rgba(255, 255, 255, 0.04)",
    "indicator_border": "rgba(255, 255, 255, 0.2)",
    "tab_hover_bg": "rgba(255, 255, 255, 0.03)",
    "scrollbar_handle": "rgba(255, 255, 255, 0.12)",
    "scrollbar_handle_hover": "rgba(255, 255, 255, 0.22)",
    "progress_bg": "rgba(255, 255, 255, 0.06)",
    "chip_bg": "rgba(255, 255, 255, 0.06)",
    "pill_bg": "rgba(255, 255, 255, 0.06)",
    "pill_bg_hover": "rgba(255, 255, 255, 0.10)",
    "pill_border_hover": "rgba(255, 255, 255, 0.18)",
    "pill_bg_pressed": "rgba(255, 255, 255, 0.14)",
    "splitter_handle": "rgba(255, 255, 255, 0.08)",
    "splitter_handle_hover": "rgba(255, 255, 255, 0.16)",

    # -- Dialog badges / notes --
    "badge_bg": "rgba(91, 124, 247, 0.16)",
    "badge_text": "#8AB4FF",
    "badge_border": "rgba(91, 124, 247, 0.32)",
    "badge_warning_bg": "rgba(243, 199, 106, 0.12)",
    "badge_warning_text": "#E9C46A",
    "badge_warning_border": "rgba(243, 199, 106, 0.28)",
    "badge_error_bg": "rgba(240, 100, 112, 0.14)",
    "badge_error_text": "#FF8A94",
    "badge_error_border": "rgba(240, 100, 112, 0.28)",
    "note_bg": "rgba(243, 199, 106, 0.10)",
    "note_border": "rgba(243, 199, 106, 0.22)",
    "note_text": "#D8BE7A",

    # -- Button roles --
    # secondary (the default QPushButton look)
    "btn_secondary_bg": "rgba(255, 255, 255, 0.05)",
    "btn_secondary_hover_bg": "rgba(255, 255, 255, 0.09)",
    "btn_secondary_hover_border": "rgba(255, 255, 255, 0.18)",
    "btn_secondary_pressed_bg": "rgba(255, 255, 255, 0.04)",
    "btn_secondary_checked_bg": "rgba(91, 124, 247, 0.15)",
    "btn_secondary_checked_text": "#8AB4FF",
    "btn_secondary_checked_border": "rgba(91, 124, 247, 0.4)",
    "btn_secondary_disabled_bg": "rgba(255, 255, 255, 0.02)",
    "btn_secondary_disabled_text": "#3A3F54",
    "btn_secondary_disabled_border": "rgba(255, 255, 255, 0.04)",
    # danger (destructive actions; secondary look with danger accents)
    "danger_border": "rgba(240, 100, 112, 0.25)",
    "danger_hover_bg": "rgba(240, 100, 112, 0.12)",
    "danger_hover_border": "rgba(240, 100, 112, 0.4)",
    "danger_hover_text": "#FF7A84",

    # -- QPalette-only --
    "palette_window_text": "#E8ECF4",
    "palette_base": "#0F1119",
    "palette_alt_base": "#1E2130",
    "palette_button": "#2A2E3C",
    "palette_link": "#5B9CF7",
    "palette_disabled": "#5A6380",

    # -- Combo box arrow icons (resolved later by rmtool._resolve_stylesheet) --
    "combo_arrow": "{arrow_dark}",
    "combo_arrow_up": "{arrow_dark_up}",
}


# ---------------------------------------------------------------------------
# Light theme
# ---------------------------------------------------------------------------

LIGHT_TOKENS = {
    **FONT_SCALE_TOKENS,
    # -- Radii / spacing --
    "radius_panel": "16px",
    "radius_inner": "12px",
    "radius_control": "8px",
    "panel_padding": "16px",

    # -- Surfaces --
    "bg_base": "#F0F2F5",
    "bg_surface": "#FFFFFF",
    "bg_inner": "#FFFFFF",
    "bg_inner_readonly": "#F5F6F8",
    "bg_popup": "#FFFFFF",
    "bg_pane": "#FFFFFF",
    "bg_panel": "#F5F6F8",
    "bg_card": "#FFFFFF",
    "bg_preview": "rgba(0, 0, 0, 0.04)",
    "bg_table": "#FFFFFF",
    "bg_table_alt": "#F8F9FB",
    "bg_header": "#F5F6F8",
    "bg_statusbar": "#FFFFFF",
    "bg_tooltip": "#FFFFFF",
    "bg_messagebox": "#F5F6F8",
    "bg_font_preview": "#F7F8FB",
    "bg_log_panel": "#F5F6F9",
    "bg_log_text": "#FFFFFF",
    "sidebar_bg": "#F5F6F8",
    "dialog_bg": "#FFFFFF",

    # -- Borders / lines --
    "border": "#DDE0E6",
    "border_subtle": "#DDE0E6",
    "border_panel": "#E3E6EC",
    "nav_item_border": "#D4D9E2",
    "border_control": "#D0D4DC",
    "border_popup": "#D0D4DC",
    "border_pane": "#DDE0E6",
    "border_dialog": "#DDE0E6",
    "border_dashed": "#D0D4DC",
    "gridline": "#ECEEF2",
    "table_row_border": "#F0F2F5",

    # -- Text --
    "text_primary": "#1A1D27",
    "text_bright": "#1A1D27",
    "text_secondary": "#3A3F50",
    "text_contrast": "#1A1D27",
    "text_muted": "#5A6070",
    "text_soft": "#6B7080",
    "text_faded": "#6B7080",
    "text_dim": "#7A8092",
    "text_pill": "#5A6070",
    "text_tab": "#8890A0",
    "text_tab_hover": "#3A3F50",
    "text_brand": "#B0B6C4",
    "text_disabled": "#A0A6B4",
    "text_readonly": "#6B7080",
    "text_subtitle": "#6B7080",
    "text_log_status": "#6E7488",
    "text_on_accent": "#FFFFFF",

    # -- Accent (single hue; used sparingly for primary actions,
    #    selection and focus) --
    "accent": "#2563EB",
    "accent_hover": "#3B82F6",
    "accent_pressed": "#1D4ED8",
    # Accent variant used by checkboxes, sliders, tabs, progress bars.
    "control_accent": "#4A6CF7",
    "accent_focus": "#4A6CF7",
    "indicator_hover_border": "#4A6CF7",

    # -- Selection --
    "selection_bg": "rgba(74, 108, 247, 0.25)",
    "selection_bg_popup": "rgba(74, 108, 247, 0.15)",
    "table_selection_bg": "rgba(74, 108, 247, 0.12)",
    "table_item_selected": "rgba(74, 108, 247, 0.10)",
    "tab_selected_bg": "rgba(74, 108, 247, 0.06)",
    "tab_hover_border": "rgba(74, 108, 247, 0.2)",
    "selection_log": "#C8CDD7",

    # -- Status --
    "success": "#2E9B61",
    "success_bg": "rgba(46, 155, 97, 0.08)",
    "success_border": "rgba(46, 155, 97, 0.25)",
    "warning": "#AF7A12",
    "danger": "#C84A5A",
    "danger_bg": "rgba(200, 74, 90, 0.10)",
    "status_on": "#3CB870",
    "status_off": "#E85B6B",

    # -- Controls --
    "btn_disabled_bg": "#E0E3E8",
    "tool_bg": "#F0F2F5",
    "tool_bg_hover": "#E4E7ED",
    "tool_border_hover": "#B0B6C4",
    "indicator_bg": "#FFFFFF",
    "indicator_border": "#B0B6C4",
    "tab_hover_bg": "rgba(0, 0, 0, 0.03)",
    "scrollbar_handle": "rgba(0, 0, 0, 0.12)",
    "scrollbar_handle_hover": "rgba(0, 0, 0, 0.22)",
    "progress_bg": "#E8EAF0",
    "chip_bg": "#F5F6F8",
    "pill_bg": "#E8EAF0",
    "pill_bg_hover": "#DDE0E6",
    "pill_border_hover": "#D0D4DC",
    "pill_bg_pressed": "#D3D7DE",
    "splitter_handle": "#E0E3E8",
    "splitter_handle_hover": "#C8CDD7",

    # -- Dialog badges / notes --
    "badge_bg": "rgba(74, 108, 247, 0.10)",
    "badge_text": "#2563EB",
    "badge_border": "rgba(74, 108, 247, 0.22)",
    "badge_warning_bg": "rgba(175, 122, 18, 0.08)",
    "badge_warning_text": "#9A6A12",
    "badge_warning_border": "rgba(175, 122, 18, 0.18)",
    "badge_error_bg": "rgba(210, 54, 72, 0.08)",
    "badge_error_text": "#C5243F",
    "badge_error_border": "rgba(210, 54, 72, 0.18)",
    "note_bg": "rgba(175, 122, 18, 0.08)",
    "note_border": "rgba(175, 122, 18, 0.18)",
    "note_text": "#7B5A18",

    # -- Button roles --
    # secondary (the default QPushButton look)
    "btn_secondary_bg": "rgba(0, 0, 0, 0.03)",
    "btn_secondary_hover_bg": "rgba(0, 0, 0, 0.05)",
    "btn_secondary_hover_border": "rgba(0, 0, 0, 0.15)",
    "btn_secondary_pressed_bg": "rgba(0, 0, 0, 0.02)",
    "btn_secondary_checked_bg": "rgba(74, 108, 247, 0.10)",
    "btn_secondary_checked_text": "#2563EB",
    "btn_secondary_checked_border": "rgba(74, 108, 247, 0.3)",
    "btn_secondary_disabled_bg": "rgba(0, 0, 0, 0.02)",
    "btn_secondary_disabled_text": "#A0A6B4",
    "btn_secondary_disabled_border": "rgba(0, 0, 0, 0.04)",
    # danger (destructive actions; secondary look with danger accents)
    "danger_border": "rgba(200, 74, 90, 0.25)",
    "danger_hover_bg": "rgba(200, 74, 90, 0.10)",
    "danger_hover_border": "rgba(200, 74, 90, 0.35)",
    "danger_hover_text": "#C84A5A",

    # -- QPalette-only --
    "palette_window_text": "#1A1D27",
    "palette_base": "#FFFFFF",
    "palette_alt_base": "#F8F9FB",
    "palette_button": "#E8EAF0",
    "palette_link": "#4A6CF7",
    "palette_disabled": "#B0B6C4",

    # -- Combo box arrow icons (resolved later by rmtool._resolve_stylesheet) --
    "combo_arrow": "{arrow_light}",
    "combo_arrow_up": "{arrow_light_up}",
}
