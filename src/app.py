from dataclasses import dataclass
from datetime import datetime
import argparse
import os
from pathlib import Path
import queue
import tempfile
import threading
import tkinter as tk
from tkinter import filedialog, font, messagebox, ttk

from tkinterdnd2 import DND_FILES, TkinterDnD

import bip_cleaner as cleaner

VERSION = '1.10'
ASSETS = Path(__file__).resolve().parent / 'assets'
BG = '#f5f7fa'
PAPER = '#ffffff'
INK = '#192436'
MUTED = '#566579'
EDGE = '#dce3eb'
BLUE = '#1458cf'
BLUE_SOFT = '#edf4ff'
FONT = 'Microsoft YaHei UI'
QUEUE_INSET = 13
QUEUE_TEXT_COLUMNS = ('name', 'status', 'reason')
LOG_DIVIDER_HEIGHT = 14
LOG_HEADER_GAP = 4
LOG_MIN_DRAG_RANGE = 32
QUEUE_CONTENT_MIN_HEIGHT = 140


@dataclass
class Row:
    path: Path
    size: int
    plan: cleaner.Plan | None = None
    output: Path | None = None
    status: str = '待分析'
    reason: str = ''


def format_size(size):
    if size < 1000:
        return f'{size:,} B'
    for unit in ('KB', 'MB', 'GB', 'TB'):
        size /= 1000
        if size < 1000 or unit == 'TB':
            return f'{size:,.1f} {unit}'


class App:
    def __init__(self, root):
        self.root = root
        self.rows = {}
        self.cache = tempfile.TemporaryDirectory(prefix='KeyShotSlim-')
        self.messages = queue.Queue()
        self.busy = False
        self.cancel = threading.Event()
        self.closing = False
        self.output = tk.StringVar()
        self.recursive = tk.BooleanVar()
        self.status = tk.StringVar(value='就绪')
        self.buttons = []
        self.stats = []
        root.title(f'KeyShot 工程瘦身 {VERSION}')
        self.logo = tk.PhotoImage(master=root, file=ASSETS / 'logo.png')
        if os.name == 'nt':
            root.iconbitmap(default=str(ASSETS / 'logo.ico'))
        else:
            root.iconphoto(True, self.logo)
        root.geometry('1120x750')
        root.minsize(920, 650)
        root.configure(bg=BG)
        root.protocol('WM_DELETE_WINDOW', self.close)
        self.setup_styles()
        self.build()
        self.refresh()
        root.after(100, self.poll)

    def setup_styles(self):
        style = ttk.Style(self.root)
        style.theme_use('clam')
        style.configure('.', font=(FONT, 10), foreground=INK)
        style.configure('TButton', padding=(11, 6), background=PAPER, bordercolor=EDGE,
                        lightcolor=PAPER, darkcolor=PAPER, focusthickness=1, focuscolor=BLUE)
        style.map('TButton', background=[('disabled', BG), ('pressed', '#e4ebf5'), ('active', '#f4f7fb')],
                  foreground=[('disabled', '#68778a')], bordercolor=[('focus', BLUE)])
        style.configure('Primary.TButton', background=BLUE, foreground=PAPER, bordercolor=BLUE,
                        focuscolor=PAPER, padding=(22, 10), font=(FONT, 11, 'bold'))
        primary_fills = [('disabled', '#e5ecf6'), ('pressed', '#1046ad'), ('active', '#1d64de'), ('!disabled', BLUE)]
        style.map('Primary.TButton', background=primary_fills, lightcolor=primary_fills, darkcolor=primary_fills,
                  bordercolor=[('disabled', '#d5dfed'), ('focus', '#092f7c'), ('!disabled', BLUE)],
                  foreground=[('disabled', '#65768d'), ('!disabled', PAPER)])
        style.configure('Quiet.TButton', background=PAPER, bordercolor=PAPER, lightcolor=PAPER, darkcolor=PAPER)
        style.configure('Analyze.TButton', background='#dbeaff', foreground='#14458d',
                        bordercolor='#8aade6', focuscolor=BLUE, padding=(20, 10), font=(FONT, 11, 'bold'))
        analyze_fills = [('disabled', '#f1f5fb'), ('pressed', '#bdd6fc'), ('active', '#ccdefa'),
                         ('!disabled', '#dbeaff')]
        style.map('Analyze.TButton', background=analyze_fills, lightcolor=analyze_fills, darkcolor=analyze_fills,
                  bordercolor=[('disabled', '#d5dfed'), ('focus', BLUE), ('!disabled', '#8aade6')],
                  foreground=[('disabled', '#65768d'), ('!disabled', '#14458d')])
        style.configure('Log.TButton', background=PAPER, foreground='#14458d',
                        bordercolor='#8aade6', focuscolor=BLUE, padding=(15, 10), font=(FONT, 11, 'bold'))
        log_fills = [('pressed', '#c5dcff'), ('active', '#e6effe'), ('selected', '#dbeaff'), ('!disabled', PAPER)]
        style.map('Log.TButton', background=log_fills, lightcolor=log_fills, darkcolor=log_fills,
                  bordercolor=[('focus', BLUE), ('active', BLUE), ('selected', '#6e9be5')])
        style.configure('TCheckbutton', background=PAPER, indicatorcolor=PAPER)
        style.map('TCheckbutton', background=[('active', PAPER)], indicatorcolor=[('selected', BLUE)])
        self.queue_font = font.Font(root=self.root, family=FONT, size=10)
        self.queue_heading_font = font.Font(root=self.root, family=FONT, size=9, weight='bold')
        style.configure('Treeview', rowheight=36, borderwidth=0, background=PAPER, fieldbackground=PAPER,
                        font=self.queue_font)
        # Tk 8.6 already reserves four pixels for data cells and one for the heading border.
        style.configure('Treeview.Cell', padding=(QUEUE_INSET - 4, 0))
        style.configure('Treeview.Heading', background='#f3f6fa', foreground=MUTED, bordercolor='#e8edf3',
                        relief='flat', padding=(QUEUE_INSET - 1, 8), font=self.queue_heading_font)
        style.map('Treeview.Heading', background=[('active', '#f3f6fa')])
        style.map('Treeview', background=[('selected', '#deebff')], foreground=[('selected', '#164788')])
        style.configure('TEntry', fieldbackground=BG, padding=(9, 5), bordercolor=EDGE,
                        lightcolor=BG, darkcolor=BG)
        style.map('TEntry', fieldbackground=[('readonly', BG)])
        style.configure('Horizontal.TProgressbar', troughcolor='#e7edf5', background=BLUE,
                        borderwidth=0, lightcolor=BLUE, darkcolor=BLUE, thickness=4)

    def label(self, parent, text='', *, bg=BG, fg=INK, size=10, bold=False, **kwargs):
        font = (FONT, size, 'bold') if bold else (FONT, size)
        return tk.Label(parent, text=text, bg=bg, fg=fg, font=font, anchor='w', **kwargs)

    def button(self, parent, name, action, *, primary=False, disable=True, style=None):
        style = style or ('Primary.TButton' if primary else 'TButton')
        button = ttk.Button(parent, text=name, command=action, style=style)
        if disable:
            self.buttons.append(button)
        return button

    def build(self):
        header = tk.Frame(self.root, bg=PAPER, padx=24, pady=14)
        header.pack(fill='x')
        tk.Label(header, image=self.logo, bg=PAPER, borderwidth=0,
                 highlightthickness=0).pack(side='left', padx=(0, 11))
        titles = tk.Frame(header, bg=PAPER)
        titles.pack(side='left')
        self.label(titles, 'KeyShot 工程瘦身', bg=PAPER, size=18, bold=True).pack(anchor='w')
        self.label(titles, '批量清理重复工作室预览', bg=PAPER, fg=MUTED, size=9).pack(anchor='w')
        self.label(header, f'适用 KeyShot Studio 2025.1 样本格式  ·  v{VERSION}',
                   bg=PAPER, fg=MUTED, size=9).pack(side='right')
        tk.Frame(self.root, bg=EDGE, height=1).pack(fill='x')

        body = tk.Frame(self.root, bg=BG, padx=24, pady=16)
        body.pack(fill='both', expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)
        self.listing = tk.Frame(body, bg=PAPER, highlightthickness=1, highlightbackground=EDGE)
        self.listing.grid(row=0, column=0, sticky='nsew')
        self.build_listing(self.listing)

        self.build_log(body)
        self.footer = tk.Frame(body, bg=BG)
        self.footer.grid(row=2, column=0, sticky='ew')
        self.build_footer(self.footer)
        body.bind('<Configure>', lambda _event: self.resize_log())
        self.log('拖入工程后可先分析文件，也可直接生成副本。双击文件行可定位文件或查看完整异常原因。')

    def build_listing(self, listing):
        toolbar = tk.Frame(listing, bg=PAPER, padx=14, pady=10)
        listing.columnconfigure(0, weight=1)
        listing.rowconfigure(2, weight=1)
        toolbar.grid(row=0, column=0, sticky='ew')
        self.label(toolbar, '文件队列', bg=PAPER, size=11, bold=True).pack(side='left', padx=(0, 14))
        for title, action in (('添加文件…', self.choose_files), ('添加文件夹…', self.choose_folder)):
            self.button(toolbar, title, action).pack(side='left', padx=(0, 6))
        subdirs = ttk.Checkbutton(toolbar, text='包括子文件夹', variable=self.recursive)
        subdirs.pack(side='left', padx=(4, 0))
        self.buttons.append(subdirs)
        clear = self.button(toolbar, '清空', self.clear)
        clear.pack(side='right')
        remove = self.button(toolbar, '移除所选', self.remove)
        remove.pack(side='right', padx=(0, 6))
        self.queue_actions = (clear, remove)
        tk.Frame(listing, bg=EDGE, height=1).grid(row=1, column=0, sticky='ew')

        self.queue_area = tk.Frame(listing, bg=PAPER)
        self.queue_area.grid(row=2, column=0, sticky='nsew')
        table = tk.Frame(self.queue_area, bg=PAPER)
        table.pack(fill='both', expand=True)
        table.columnconfigure(0, weight=1)
        table.rowconfigure(0, weight=1)
        columns = ('name', 'before', 'saved', 'after', 'status', 'reason')
        self.tree = ttk.Treeview(table, columns=columns, show='headings', selectmode='extended')
        for key, title, preferred, minimum, anchor in (
                ('name', '文件名', 290, 155, 'w'),
                ('before', '原始大小', 108, 108, 'e'),
                ('saved', '可释放空间', 108, 108, 'e'),
                ('after', '瘦身后大小', 108, 108, 'e'),
                ('status', '状态', 136, 136, 'w'),
                ('reason', '异常原因', 228, 135, 'w')):
            minimum = max(minimum, self.queue_heading_font.measure(title) + 2 * QUEUE_INSET)
            if anchor == 'e':
                minimum = max(minimum, self.queue_font.measure('999.9 GB') + 2 * QUEUE_INSET)
            elif key == 'status':
                minimum = max(minimum, self.queue_font.measure('副本已生成') + 2 * QUEUE_INSET)
            self.tree.heading(key, text=title, anchor=anchor)
            self.tree.column(key, width=max(preferred, minimum), minwidth=minimum, anchor=anchor,
                             stretch=key in ('name', 'reason'))
        vertical = ttk.Scrollbar(table, orient='vertical', command=self.tree.yview)
        self.queue_horizontal = ttk.Scrollbar(table, orient='horizontal', command=self.tree.xview)
        self.tree.configure(yscrollcommand=vertical.set, xscrollcommand=self.update_queue_scroll)
        self.tree.grid(row=0, column=0, sticky='nsew')
        vertical.grid(row=0, column=1, sticky='ns')
        self.queue_horizontal.grid(row=1, column=0, columnspan=2, sticky='ew')
        self.queue_horizontal.grid_remove()
        self.queue_text_widths = self.queue_column_widths()
        self.queue_initial_fit = True
        self.queue_layout_job = None
        for event in ('<Configure>', '<B1-Motion>', '<ButtonRelease-1>'):
            self.tree.bind(event, self.schedule_queue_layout, add=True)
        self.tree.tag_configure('error', background='#fff5f3')
        self.tree.tag_configure('even', background=PAPER)
        self.tree.tag_configure('odd', background='#f8fafc')
        self.tree.bind('<Double-1>', self.show_file)
        self.tree.bind('<Delete>', lambda _event: self.remove())
        self.tree.bind('<<TreeviewSelect>>', lambda _event: self.update_queue_actions())

        self.drop_panel = tk.Canvas(self.queue_area, bg=PAPER, highlightthickness=0, cursor='hand2',
                                    takefocus=True)
        self.drop_panel.bind('<Configure>', self.draw_drop_panel)
        self.drop_panel.bind('<FocusIn>', self.draw_drop_panel)
        self.drop_panel.bind('<FocusOut>', self.draw_drop_panel)
        for event in ('<Button-1>', '<Return>', '<space>'):
            self.drop_panel.bind(event, lambda _event: self.choose_files() if not self.busy else None)
        for widget in (self.root, self.drop_panel, self.tree):
            widget.drop_target_register(DND_FILES)
            widget.dnd_bind('<<Drop>>', self.drop)

        tk.Frame(listing, bg=EDGE, height=1).grid(row=3, column=0, sticky='ew')
        metrics = tk.Frame(listing, bg=PAPER, padx=14, pady=8)
        metrics.grid(row=4, column=0, sticky='ew')
        for column, title in enumerate(('队列文件', '原始总大小', '可释放空间', '瘦身后大小')):
            if column:
                tk.Frame(metrics, bg=EDGE, width=1, height=15).pack(side='left', padx=12)
            self.label(metrics, title, bg=PAPER, fg=MUTED, size=9).pack(side='left', padx=(0, 7))
            value = tk.StringVar()
            self.label(metrics, bg=PAPER, fg=BLUE if column == 2 else INK, size=10, bold=True,
                       textvariable=value).pack(side='left')
            self.stats.append(value)

    def draw_drop_panel(self, _event=None):
        canvas = self.drop_panel
        width, height = canvas.winfo_width(), canvas.winfo_height()
        if width < 180 or height < 100:
            return
        canvas.delete('all')
        center_x, center_y = width / 2, height / 2
        half_width, half_height = min(247, width / 2 - 20), min(85, height / 2 - 12)
        focused = canvas.focus_get() == canvas
        canvas.create_rectangle(center_x - half_width, center_y - half_height, center_x + half_width,
                                center_y + half_height, fill='#fbfcfe', outline=BLUE if focused else '#b5c5da',
                                dash=(4, 4), width=2 if focused else 1)
        icon_y = center_y - 35
        canvas.create_oval(center_x - 19, icon_y - 19, center_x + 19, icon_y + 19,
                           fill=BLUE_SOFT, outline='')
        canvas.create_line(center_x - 9, icon_y + 3, center_x - 9, icon_y + 8, center_x + 9, icon_y + 8,
                           center_x + 9, icon_y + 3, fill=BLUE, width=2, joinstyle='round')
        canvas.create_line(center_x, icon_y + 3, center_x, icon_y - 8, fill=BLUE, width=2)
        canvas.create_line(center_x - 4, icon_y - 4, center_x, icon_y - 8, center_x + 4, icon_y - 4,
                           fill=BLUE, width=2, joinstyle='round')
        canvas.create_text(center_x, center_y + 4, text='拖入 KeyShot 工程', fill=INK, font=(FONT, 13, 'bold'))
        canvas.create_text(center_x, center_y + 31, text='支持 BIP、KSP 文件或文件夹', fill=MUTED, font=(FONT, 10))

    def build_footer(self, workspace):
        dest = tk.Frame(workspace, bg=BG)
        dest.pack(fill='x', pady=(12, 0))
        self.label(dest, '输出位置', bold=True, size=9).pack(side='left', padx=(0, 10))
        self.dest_entry = ttk.Entry(dest, state='readonly')
        self.dest_entry.pack(side='left', fill='x', expand=True)
        self.button(dest, '更改…', self.change_output).pack(side='left', padx=(7, 0))
        self.button(dest, '使用源目录', lambda: self.output.set('')).pack(side='left', padx=(7, 0))
        self.button(dest, '打开目录', self.open_output, disable=False).pack(side='left', padx=(7, 0))
        self.output.trace_add('write', lambda *_: self.show_destination())
        self.show_destination()
        actions = tk.Frame(workspace, bg=PAPER, padx=10, pady=8,
                           highlightthickness=1, highlightbackground=EDGE)
        actions.pack(fill='x', pady=(9, 6))
        actions.columnconfigure(1, weight=1)
        self.log_icon = tk.PhotoImage(master=actions, width=16, height=16)
        for box in ((2, 1, 14, 2), (2, 14, 14, 15), (2, 2, 3, 14), (13, 2, 14, 14),
                    (5, 5, 11, 6), (5, 8, 11, 9), (5, 11, 9, 12)):
            self.log_icon.put('#164788', to=box)
        self.log_button = self.button(actions, '查看处理日志', self.toggle_log, disable=False, style='Log.TButton')
        self.log_button.configure(image=self.log_icon, compound='left')
        self.log_button.grid(row=0, column=0, sticky='ns')
        status = self.label(actions, bg=PAPER, fg=MUTED, size=9, width=1, textvariable=self.status)
        status.grid(row=0, column=1, sticky='ew', padx=(12, 8))
        self.cancel_button = self.button(actions, '取消处理', self.stop, disable=False, style='Quiet.TButton')
        self.cancel_button.grid(row=0, column=2, padx=(0, 8))
        self.analyze_button = self.button(actions, '分析文件', lambda: self.start(False), style='Analyze.TButton')
        self.analyze_button.grid(row=0, column=3, sticky='ns', padx=(0, 10))
        self.make_button = self.button(actions, '生成瘦身副本', lambda: self.start(True), primary=True)
        self.make_button.grid(row=0, column=4, sticky='ns')
        self.cancel_button.state(['disabled'])
        self.progress = ttk.Progressbar(workspace, mode='determinate')
        self.progress.pack(fill='x')

    def build_log(self, body):
        self.log_visible = False
        self.log_height = None
        self.log_pane = tk.Frame(body, bg=BG)
        self.log_pane.grid(row=1, column=0, sticky='ew')
        self.log_pane.grid_remove()

        self.log_divider = tk.Canvas(self.log_pane, height=LOG_DIVIDER_HEIGHT, bg=BG,
                                     highlightthickness=0, cursor='sb_v_double_arrow')
        self.log_divider.pack(fill='x')
        mid = LOG_DIVIDER_HEIGHT // 2
        line = self.log_divider.create_line(0, mid, 0, mid, fill=EDGE)
        grip = self.log_divider.create_line(0, mid, 0, mid, fill='#8095b1', width=3, capstyle='round')

        def position_grip(event):
            center = event.width // 2
            self.log_divider.coords(line, 0, mid, event.width, mid)
            self.log_divider.coords(grip, center - 23, mid, center + 23, mid)

        self.log_divider.bind('<Configure>', position_grip)
        self.log_divider.bind('<Enter>', lambda _event: self.log_divider.itemconfigure(grip, fill=BLUE))
        self.log_divider.bind('<Leave>', lambda _event: self.log_divider.itemconfigure(grip, fill='#8095b1'))
        self.log_divider.bind('<ButtonPress-1>', self.start_log_drag)
        self.log_divider.bind('<B1-Motion>', self.drag_log)

        self.log_header = tk.Frame(self.log_pane, bg=BG)
        self.log_header.pack(fill='x', pady=(0, LOG_HEADER_GAP))
        self.label(self.log_header, '处理日志', bold=True, size=9).pack(side='left')
        self.log_panel = tk.Frame(self.log_pane, bg=PAPER, highlightthickness=1, highlightbackground=EDGE)
        self.log_panel.pack(fill='x')
        self.log_panel.pack_propagate(False)
        self.logbox = tk.Text(self.log_panel, height=6, bg=PAPER, fg=MUTED, wrap='word', padx=10, pady=6,
                              relief='flat', font=(FONT, 9), state='disabled')
        self.logbox.pack(side='left', fill='both', expand=True)
        log_scrollbar = ttk.Scrollbar(self.log_panel, orient='vertical', command=self.logbox.yview)
        log_scrollbar.pack(side='right', fill='y')
        self.logbox.configure(yscrollcommand=log_scrollbar.set)

    def start_log_drag(self, event):
        self.log_drag_y = event.y_root
        self.log_drag_height = self.log_panel.winfo_height()

    def drag_log(self, event):
        self.resize_log(self.log_drag_height + self.log_drag_y - event.y_root)

    def resize_log(self, height=None):
        if not self.log_visible:
            return
        body = self.log_pane.master
        body_padding = 2 * body.winfo_pixels(body.cget('pady'))
        # Reserve the queue toolbar, summary and enough room for the table or drop target.
        queue_min = (self.listing.winfo_reqheight() - self.queue_area.winfo_reqheight()
                     + QUEUE_CONTENT_MIN_HEIGHT)
        log_chrome = LOG_DIVIDER_HEIGHT + self.log_header.winfo_reqheight() + LOG_HEADER_GAP
        minimum = max(52, self.logbox.winfo_reqheight() // 2)
        fixed_height = body.winfo_y() + body_padding + self.footer.winfo_reqheight() + queue_min + log_chrome
        # Larger Windows text needs a taller minimum window to keep both panes usable.
        minimum_window_height = max(650, fixed_height + minimum + LOG_MIN_DRAG_RANGE)
        if self.root.minsize()[1] != minimum_window_height:
            self.root.minsize(920, minimum_window_height)
        maximum = max(minimum, self.root.winfo_height() - fixed_height)
        preferred = self.log_height if height is None else height
        if preferred is None:
            preferred = min(self.logbox.winfo_reqheight() + 2, maximum - (maximum - minimum) // 4)
        visible_height = max(minimum, min(maximum, preferred))
        if height is not None:
            self.log_height = visible_height
        self.log_panel.configure(height=visible_height)

    def toggle_log(self):
        self.log_visible = not self.log_visible
        if not self.log_visible:
            self.log_pane.grid_remove()
            self.log_button.configure(text='查看处理日志')
            self.log_button.state(['!selected'])
        else:
            self.resize_log()
            self.log_pane.grid()
            self.log_button.configure(text='收起处理日志')
            self.log_button.state(['selected'])
            self.logbox.see('end')

    def update_queue_actions(self):
        clear, remove = self.queue_actions
        clear.state(['!disabled'] if self.rows and not self.busy else ['disabled'])
        remove.state(['!disabled'] if self.tree.selection() and not self.busy else ['disabled'])

    def show_destination(self):
        displayed = self.output.get() or '与每个源文件同目录（默认）'
        self.dest_entry.configure(state='normal')
        self.dest_entry.delete(0, 'end')
        self.dest_entry.insert(0, displayed)
        self.dest_entry.configure(state='readonly')

    def log(self, message):
        self.logbox.configure(state='normal')
        self.logbox.insert('end', f'[{datetime.now():%H:%M:%S}] {message}\n')
        self.logbox.see('end')
        self.logbox.configure(state='disabled')

    def refresh(self):
        if self.rows and self.drop_panel.place_info():
            self.drop_panel.place_forget()
        elif not self.rows and not self.drop_panel.place_info():
            self.drop_panel.place(x=0, y=0, relwidth=1, relheight=1)
        self.update_queue_actions()
        self.stats[0].set(f'{len(self.rows)} 个')
        self.stats[1].set(format_size(sum(row.size for row in self.rows.values())))
        analyzed = [row for row in self.rows.values() if row.plan is not None]
        self.stats[2].set(format_size(sum(max(0, row.plan.saved) for row in analyzed)) if analyzed else '待分析')
        self.stats[3].set(format_size(sum(min(row.size, row.plan.after) for row in analyzed)) if analyzed else '待分析')
        if not self.busy:
            for button in (self.analyze_button, self.make_button):
                button.state(['!disabled'] if self.rows else ['disabled'])

    def update_queue_scroll(self, first, last):
        self.queue_horizontal.set(first, last)
        overflow = float(first) > 0 or float(last) < 1
        if overflow and not self.queue_horizontal.grid_info():
            self.queue_horizontal.grid()
        elif not overflow and self.queue_horizontal.grid_info():
            self.queue_horizontal.grid_remove()
        self.schedule_queue_layout()

    def queue_column_widths(self):
        return {key: self.tree.column(key, 'width') for key in QUEUE_TEXT_COLUMNS}

    def schedule_queue_layout(self, _event=None):
        if self.queue_layout_job is None:
            self.queue_layout_job = self.root.after_idle(self.update_queue_layout)

    def update_queue_layout(self):
        self.queue_layout_job = None
        if self.queue_initial_fit and self.tree.winfo_width() > 1:
            self.queue_initial_fit = False
            self.fit_initial_queue_width()
        widths = self.queue_column_widths()
        if widths == self.queue_text_widths:
            return
        self.queue_text_widths = widths
        for key, row in self.rows.items():
            for column, value in self.queue_text(row).items():
                if self.tree.set(key, column) != value:
                    self.tree.set(key, column, value)

    def fit_initial_queue_width(self):
        # Treeview may keep its requested column widths on its first layout.
        first, last = self.tree.xview()
        if last - first >= 1:
            return
        total = sum(self.tree.column(column, 'width') for column in self.tree['columns'])
        remaining = total - round(total * (last - first))
        for column, share in (('name', (remaining + 1) // 2), ('reason', remaining // 2),
                              ('name', None), ('reason', None)):
            current = self.tree.column(column, 'width')
            minimum = self.tree.column(column, 'minwidth')
            reduction = min(remaining if share is None else share, current - minimum)
            if reduction:
                self.tree.column(column, width=current - reduction)
                remaining -= reduction
        self.tree.xview_moveto(0)

    def fit_queue_text(self, value, width):
        value = ' '.join(value.splitlines())
        available = width - 2 * QUEUE_INSET
        measure = self.queue_font.measure
        if measure(value) <= available:
            return value
        if measure('…') > available:
            return ''
        low, high = 0, len(value)
        while low < high:
            length = (low + high + 1) // 2
            if measure(value[:length] + '…') <= available:
                low = length
            else:
                high = length - 1
        return value[:low].rstrip() + '…'

    def queue_text(self, row):
        return {
            column: self.fit_queue_text(value, self.queue_text_widths[column])
            for column, value in zip(QUEUE_TEXT_COLUMNS, (row.path.name, row.status, row.reason or '—'))
        }

    def refresh_row(self, key):
        row = self.rows[key]
        text = self.queue_text(row)
        values = (text['name'], format_size(row.size),
                  format_size(max(0, row.plan.saved)) if row.plan else '—',
                  format_size(min(row.size, row.plan.after)) if row.plan else '—',
                  text['status'], text['reason'])
        if self.tree.exists(key):
            stripe = self.tree.item(key, 'tags')[-1]
            tags = ('error', stripe) if row.reason else (stripe,)
            self.tree.item(key, values=values, tags=tags)
        else:
            stripe = 'odd' if len(self.tree.get_children()) % 2 else 'even'
            self.tree.insert('', 'end', iid=key, values=values, tags=(stripe,))
        self.refresh()

    def drop(self, event):
        if self.busy:
            return 'refuse_drop'
        self.add(self.root.tk.splitlist(event.data))
        return 'copy'

    def choose_files(self):
        self.add(filedialog.askopenfilenames(parent=self.root, title='选择 KeyShot 工程', filetypes=[('KeyShot 工程', ('*.bip', '*.ksp')), ('BIP 场景', '*.bip'), ('KSP 工程包', '*.ksp')]))

    def choose_folder(self):
        folder = filedialog.askdirectory(parent=self.root, title='选择包含 BIP 或 KSP 工程的文件夹')
        if folder:
            self.add([folder])

    def add(self, paths):
        added = 0
        for name in paths:
            try:
                path = Path(name)
                files = path.rglob('*') if path.is_dir() and self.recursive.get() else path.iterdir() if path.is_dir() else [path]
                for candidate in files:
                    if candidate.suffix.lower() not in cleaner.FILE_SUFFIXES or not candidate.is_file():
                        continue
                    resolved = candidate.resolve(strict=True)
                    key = os.path.normcase(str(resolved))
                    if key not in self.rows:
                        self.rows[key] = Row(resolved, resolved.stat().st_size)
                        self.refresh_row(key)
                        added += 1
            except OSError as error:
                self.log(f'读取路径失败 · {error}')
        if paths:
            self.log(f'已添加 {added} 个 KeyShot 工程' if added else '没有找到可添加的 BIP 或 KSP 工程。')

    def remove(self, keys=None):
        if self.busy:
            return
        for key in self.tree.selection() if keys is None else keys:
            row = self.rows.pop(key)
            if row.plan and row.plan.replacement:
                row.plan.replacement.unlink(missing_ok=True)
            self.tree.delete(key)
        self.refresh()

    def clear(self):
        self.remove(tuple(self.rows))

    def change_output(self):
        folder = filedialog.askdirectory(parent=self.root, title='选择瘦身副本输出目录')
        if folder:
            self.output.set(folder)
            self.log('已切换输出目录。若工程使用相对路径贴图，请复开副本核对资源。')

    def show_file(self, event):
        key = self.tree.identify_row(event.y)
        if key:
            row = self.rows[key]
            if row.reason:
                messagebox.showinfo('异常原因', f'{row.path.name}\n\n{row.reason}', parent=self.root)
            else:
                self.explore(row.output or row.path)

    def explore(self, path):
        try:
            import subprocess
            path = Path(path)
            if path.is_file():
                subprocess.Popen(['explorer.exe', '/select,', str(path)])
            else:
                os.startfile(path)
        except OSError as error:
            self.log(f'打开位置失败 · {error}')

    def open_output(self):
        key = next(iter(self.tree.selection()), None)
        row = self.rows.get(key) or next(iter(self.rows.values()), None)
        fallback = Path(Path.home(), 'Documents')
        if not fallback.is_dir():
            fallback = Path.home()
        self.explore(self.output.get() or ((row.output or row.path).parent if row else fallback))

    def start(self, make):
        if not self.rows or self.busy:
            return
        self.busy = True
        for button in self.buttons:
            button.state(['disabled'])
        self.cancel_button.state(['!disabled'])
        self.cancel.clear()
        self.progress.configure(mode='indeterminate')
        self.progress.start(12)
        self.status.set('正在处理…')
        self.log('开始生成副本。' if make else '开始分析文件。')
        rows = [(key, row.path, row.plan) for key, row in self.rows.items()]
        threading.Thread(target=self.work, args=(rows, self.output.get(), make), daemon=True).start()

    def stop(self):
        self.cancel.set()
        self.cancel_button.state(['disabled'])
        self.status.set('正在结束当前操作…')

    def work(self, rows, dest, make):
        def check():
            if self.cancel.is_set():
                raise cleaner.Cancelled()

        post = self.messages.put
        try:
            for index, (key, path, plan) in enumerate(rows, 1):
                try:
                    check()
                    if plan is not None and cleaner.fingerprint(path.stat()) != plan.original:
                        if plan.replacement:
                            plan.replacement.unlink(missing_ok=True)
                        plan = None
                        post(('invalidate', key))
                    if plan is None:
                        workdir = Path(tempfile.mkdtemp(dir=self.cache.name))
                        def stage(message):
                            post(('stage', key, f'{index}/{len(rows)} · {message}'))
                        plan = cleaner.analyze(path, workdir, stage, check)
                        post(('analyzed', key, plan))
                    result = None
                    if make and plan.saved > 0:
                        post(('stage', key, f'{index}/{len(rows)} · 写入瘦身副本'))
                        result = cleaner.generate(plan, dest, check)
                    post(('result', key, plan, result))
                except cleaner.Cancelled:
                    post(('cancelled', key))
                    break
                except (OSError, ValueError, cleaner.zstd.ZstdError) as error:
                    post(('error', key, str(error), isinstance(error, cleaner.Unsupported)))
                except Exception as error:
                    post(('error', key, f'处理失败 · {error}', False))
        finally:
            post(('done',))

    def poll(self):
        while True:
            try:
                action, *args = self.messages.get_nowait()
            except queue.Empty:
                break
            if action == 'done':
                self.busy = False
                self.progress.stop()
                self.progress.configure(mode='determinate', value=0)
                self.status.set('已取消' if self.cancel.is_set() else '处理结束')
                for button in self.buttons:
                    button.state(['!disabled'])
                self.cancel_button.state(['disabled'])
                self.refresh()
                if self.closing:
                    self.close()
                    return
                continue
            key, *payload = args
            row = self.rows[key]
            if action == 'stage':
                row.reason = ''
                row.status = payload[0]
                self.status.set(f'{row.path.name} · {row.status}')
            elif action == 'invalidate':
                row.plan = None
                row.output = None
            elif action == 'analyzed':
                row.plan = payload[0]
                row.size = row.plan.before
            elif action == 'result':
                plan, result = payload
                row.plan, row.size = plan, plan.before
                row.reason = ''
                if result:
                    row.output = result
                    row.status = '副本已生成'
                    self.log(f'{row.path.name} · {format_size(plan.before)} → {format_size(plan.after)} · {result}')
                elif plan.saved > 0:
                    row.status = '已有副本' if row.output else '分析完成'
                    self.log(f'{row.path.name} · 找到 {plan.removed:,} 份重复预览，预计缩小 {plan.saved / plan.before:.1%}')
                else:
                    row.status = '无需瘦身'
                    detail = '重新打包后体积未减少。' if plan.removed else '没有可缩小体积的重复预览。'
                    self.log(f'{row.path.name} · {detail}')
            elif action == 'error':
                error, unsupported = payload
                row.status = '无法处理' if unsupported else '处理失败'
                row.reason = error
                self.log(f'{row.path.name} · {error}')
            elif action == 'cancelled':
                row.status = '已取消'
                row.reason = ''
            self.refresh_row(key)
        self.root.after(100, self.poll)

    def close(self):
        if self.busy:
            if messagebox.askyesno('取消并关闭', '还有文件正在处理，是否取消并关闭窗口？', parent=self.root):
                self.closing = True
                self.stop()
            return
        self.cache.cleanup()
        self.root.destroy()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('files', nargs='*')
    parser.add_argument('--self-test', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    root = TkinterDnD.Tk()
    app = App(root)
    if args.files:
        app.add(args.files)
    if args.self_test:
        root.update()
        args.self_test.write_text(f'Drag-and-drop ready: {root.TkdndVersion}\nRows: {len(app.rows)}\n', encoding='utf-8')
        app.close()
    else:
        root.mainloop()


if __name__ == '__main__':
    main()


