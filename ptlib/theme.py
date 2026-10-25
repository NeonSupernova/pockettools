"""Shared palette and widget factories."""
import ui

BG = '#101317'
PANEL = '#1a1f26'
PANEL_HI = '#262d36'
TEXT = '#e8e8e8'
MUTED = '#8b949e'
AMBER = '#ffb000'
CYAN = '#3fd0e0'
GREEN = '#4cd97b'
RED = '#ff5c5c'
SERIES = [CYAN, AMBER, GREEN, '#c77dff', RED, '#5c9dff', '#ff8fd1', '#b5e853']

FONT = '<system>'
BOLD = '<system-bold>'
MONO = 'Menlo'


def button(title, action=None, bg=PANEL, fg=TEXT, size=17, bold=True, radius=10):
    b = ui.Button(title=title)
    if action is not None:
        b.action = action
    b.background_color = bg
    b.tint_color = fg
    b.font = (BOLD if bold else FONT, size)
    b.corner_radius = radius
    return b


def label(text='', size=15, color=TEXT, bold=False, align=ui.ALIGN_CENTER, mono=False, lines=1):
    lb = ui.Label()
    lb.text = text
    lb.text_color = color
    lb.font = (MONO if mono else (BOLD if bold else FONT), size)
    lb.alignment = align
    lb.number_of_lines = lines
    return lb


def table(owner, row_height=56):
    tv = ui.TableView()
    tv.background_color = BG
    tv.separator_color = PANEL_HI
    tv.row_height = row_height
    tv.data_source = owner
    tv.delegate = owner
    return tv


def cell(title, detail=None, style='subtitle', fg=TEXT, detail_fg=MUTED, bg=BG):
    c = ui.TableViewCell(style)
    c.background_color = bg
    c.text_label.text = title
    c.text_label.text_color = fg
    if detail is not None and c.detail_text_label is not None:
        c.detail_text_label.text = detail
        c.detail_text_label.text_color = detail_fg
    c.selected_background_view = ui.View(background_color=PANEL_HI)
    return c
