"""Run with python path_companion/app.py. All processing stays local."""
import json
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from PIL import Image, ImageTk
from core import GROUPS, load_package, reconstruct, write_svg


class App:
    def __init__(self, root):
        self.root = root
        root.title('Lazy Path Companion — prototype')
        root.geometry('1250x850')
        self.pool = ThreadPoolExecutor(max_workers=1)
        self.folder = None
        self.paths = None
        self.hidden = set()
        self.selected = None
        self.scale = 1
        self.offset = (0, 0)
        controls = ttk.Frame(root, padding=12)
        controls.pack(side='left', fill='y')
        ttk.Button(controls, text='Open Blender package…', command=self.open).pack(fill='x')
        self.variables = {}
        for key, label, default in [('threshold', 'Mask threshold (0–1)', .5),
                                    ('gap', 'Join geometry gaps (pixels)', 0),
                                    ('tolerance', 'Simplify tolerance (pixels)', .6),
                                    ('minimum', 'Minimum joined length (pixels)', 0)]:
            ttk.Label(controls, text=label).pack(anchor='w', pady=(9, 0))
            var = tk.StringVar(value=str(default))
            self.variables[key] = var
            ttk.Entry(controls, textvariable=var).pack(fill='x')
        self.process_button = ttk.Button(controls, text='Reconstruct paths', command=self.process)
        self.process_button.pack(fill='x', pady=12)
        self.styles = {}
        for group in GROUPS:
            enabled = tk.BooleanVar(value=True)
            width = tk.StringVar(value='1.5')
            opacity = tk.StringVar(value='1')
            self.styles[group] = (enabled, width, opacity)
            ttk.Checkbutton(controls, text=group.replace('_', ' ').title(), variable=enabled,
                            command=self.draw).pack(anchor='w')
            row = ttk.Frame(controls)
            row.pack(fill='x')
            ttk.Label(row, text='Width').pack(side='left')
            ttk.Entry(row, textvariable=width, width=6).pack(side='left')
            ttk.Label(row, text='Opacity').pack(side='left')
            ttk.Entry(row, textvariable=opacity, width=6).pack(side='left')
        ttk.Button(controls, text='Apply group styles', command=self.draw).pack(fill='x', pady=8)
        self.background = tk.BooleanVar(value=True)
        self.original = tk.BooleanVar(value=False)
        self.show_joins = tk.BooleanVar(value=True)
        for label, var in [('Show colour render', self.background), ('Show original paths', self.original),
                           ('Highlight joins in orange', self.show_joins)]:
            ttk.Checkbutton(controls, text=label, variable=var, command=self.draw).pack(anchor='w')
        ttk.Label(controls, text='Click a path to select it.\nWheel to zoom; right-drag to pan.').pack(pady=10)
        ttk.Button(controls, text='Hide selected path', command=self.hide).pack(fill='x')
        ttk.Button(controls, text='Restore hidden paths', command=self.restore).pack(fill='x')
        ttk.Button(controls, text='Fit image', command=self.fit).pack(fill='x')
        ttk.Button(controls, text='Save SVG + editable session…', command=self.save).pack(fill='x', pady=10)
        ttk.Button(controls, text='Load saved session…', command=self.load_session).pack(fill='x')
        self.canvas = tk.Canvas(root, background='#d9d9d9', highlightthickness=0)
        self.canvas.pack(side='top', fill='both', expand=True)
        self.status = tk.StringVar(value='Open a folder created by Export for Path Processing in Blender.')
        ttk.Label(root, textvariable=self.status, wraplength=900, padding=8).pack(fill='x')
        self.canvas.bind('<Button-1>', self.select)
        self.canvas.bind('<MouseWheel>', self.zoom)
        self.canvas.bind('<Button-3>', self.pan_start)
        self.canvas.bind('<B3-Motion>', self.pan)
        root.protocol('WM_DELETE_WINDOW', self.close)

    def close(self):
        self.pool.shutdown(wait=False, cancel_futures=True)
        self.root.destroy()

    def open(self):
        if getattr(self, 'busy', False):
            return
        folder = filedialog.askdirectory(title='Choose the folder containing package.json')
        if not folder:
            return
        try:
            data = load_package(folder)
            image = Image.open(Path(folder) / data['files']['flats']).convert('RGBA')
        except Exception as exc:
            messagebox.showerror('Cannot open package', str(exc))
            return
        self.folder, self.data, self.image = folder, data, image
        self.paths = None
        self.hidden.clear()
        self.fit()
        self.process()

    def process(self):
        if not self.folder or getattr(self, 'busy', False):
            return
        try:
            settings = {key: float(var.get()) for key, var in self.variables.items()}
            if not .001 <= settings['threshold'] <= .999 or any(not 0 <= settings[k] <= 10000 for k in ('gap', 'tolerance', 'minimum')):
                raise ValueError('Use threshold 0.001–0.999 and nonnegative pixel settings up to 10000.')
        except ValueError as exc:
            messagebox.showerror('Invalid settings', str(exc))
            return
        self.busy = True
        self.process_button.state(['disabled'])
        self.status.set('Tracing masks and reconstructing geometry…')
        future = self.pool.submit(reconstruct, self.folder, self.data, **settings)
        def poll():
            if not future.done():
                self.root.after(100, poll)
                return
            self.busy = False
            self.process_button.state(['!disabled'])
            try:
                self.raw, self.paths, self.joins = future.result()
                self.applied_settings = settings
                self.hidden.clear()
                self.selected = None
                self.status.set(' | '.join(f'{g}: {len(self.raw[g])} → {len(self.paths[g])} paths' for g in GROUPS)
                                + '. Reconstructing resets local hides. Joins are inferred, not occlusion-aware.')
                self.draw()
            except Exception as exc:
                self.status.set('Processing failed')
                messagebox.showerror('Processing failed', str(exc))
        poll()

    def get_styles(self):
        styles = {}
        for group, (enabled, width, opacity) in self.styles.items():
            w, o = float(width.get()), float(opacity.get())
            if not 0 < w <= 1000 or not 0 <= o <= 1:
                raise ValueError('Widths must be 0–1000 (excluding zero); opacity must be 0–1.')
            styles[group] = dict(enabled=enabled.get(), width=w, opacity=o, color='#171717')
        return styles

    def fit(self):
        if not self.folder:
            return
        self.root.update_idletasks()
        self.scale = min(max(1, self.canvas.winfo_width() - 30) / self.data['width'],
                         max(1, self.canvas.winfo_height() - 30) / self.data['height'])
        self.offset = (15, 15)
        self.draw()

    def draw(self):
        self.canvas.delete('all')
        if not self.folder:
            return
        try:
            styles = self.get_styles()
        except ValueError:
            self.status.set('Enter valid group widths and opacities, then Apply group styles.')
            return
        ox, oy = self.offset
        if self.background.get():
            # Crop before resizing so deep zoom never allocates a huge bitmap.
            w, h = self.canvas.winfo_width(), self.canvas.winfo_height()
            extent = (-ox / self.scale, -oy / self.scale, (w - ox) / self.scale, (h - oy) / self.scale)
            display = self.image.transform((max(1, w), max(1, h)), Image.Transform.EXTENT, extent,
                                           resample=Image.Resampling.BILINEAR)
            plate = Image.new('RGBA', display.size, 'white')
            plate.alpha_composite(display)
            self.photo = ImageTk.PhotoImage(plate.convert('RGB'))
            self.canvas.create_image(0, 0, image=self.photo, anchor='nw')
        if self.paths is None:
            return
        paths = self.raw if self.original.get() else self.paths
        for group, lines in paths.items():
            style = styles[group]
            if not style['enabled']:
                continue
            # Tk lines do not support true alpha; approximate over white in preview.
            shade = round(255 - (255 - 23) * style['opacity'])
            color = '#%02x%02x%02x' % (shade, shade, shade)
            for index, points in enumerate(lines):
                if not self.original.get() and (group, index) in self.hidden:
                    continue
                coordinates = [value for x, y in points for value in (ox + x * self.scale, oy + y * self.scale)]
                self.canvas.create_line(*coordinates, fill='#008dc4' if self.selected == (group, index) else color,
                                        width=max(.5, style['width'] * self.scale), capstyle='round', joinstyle='round',
                                        tags=('path', f'{group}:{index}'))
        if self.show_joins.get() and not self.original.get():
            for joins in self.joins.values():
                for a, b in joins:
                    self.canvas.create_line(ox + a[0] * self.scale, oy + a[1] * self.scale,
                                            ox + b[0] * self.scale, oy + b[1] * self.scale,
                                            fill='#ef7d00', width=3)

    def select(self, event):
        if self.original.get():
            return
        for item in reversed(self.canvas.find_overlapping(event.x - 4, event.y - 4, event.x + 4, event.y + 4)):
            tags = self.canvas.gettags(item)
            if 'path' in tags:
                group, index = tags[1].split(':')
                self.selected = group, int(index)
                self.draw()
                return

    def hide(self):
        if self.selected:
            self.hidden.add(self.selected)
            self.selected = None
            self.draw()

    def restore(self):
        self.hidden.clear()
        self.draw()

    def zoom(self, event):
        old = self.scale
        self.scale = min(20, max(.02, old * (1.2 if event.delta > 0 else 1 / 1.2)))
        ox, oy = self.offset
        ratio = self.scale / old
        self.offset = (event.x - (event.x - ox) * ratio, event.y - (event.y - oy) * ratio)
        self.draw()

    def pan_start(self, event):
        self.drag = event.x, event.y, self.offset

    def pan(self, event):
        x, y, (ox, oy) = self.drag
        self.offset = ox + event.x - x, oy + event.y - y
        self.draw()

    def save(self):
        if self.paths is None or getattr(self, 'busy', False):
            return
        try:
            styles = self.get_styles()
            file = filedialog.asksaveasfilename(defaultextension='.svg', filetypes=[('SVG paths', '*.svg')])
            if not file:
                return
            write_svg(file, self.data, self.paths, styles, self.hidden)
            session = dict(version=1, width=self.data['width'], height=self.data['height'],
                           package_id=self.data['package_id'],
                           paths=self.paths, raw=self.raw, joins=self.joins, styles=styles,
                           hidden=list(self.hidden), settings=self.applied_settings)
            Path(file).with_suffix('.paths.json').write_text(json.dumps(session), encoding='utf-8')
            self.status.set('Saved SVG lines and editable .paths.json session. Colour background is not embedded.')
        except Exception as exc:
            messagebox.showerror('Cannot save', str(exc))

    def load_session(self):
        if not self.folder or getattr(self, 'busy', False):
            messagebox.showinfo('Open package first', 'Open the original Blender package before loading its session.')
            return
        file = filedialog.askopenfilename(filetypes=[('Path session', '*.paths.json')])
        if not file:
            return
        try:
            session = json.loads(Path(file).read_text(encoding='utf-8'))
            if session['version'] != 1 or session.get('package_id') != self.data['package_id']:
                raise ValueError('This session belongs to a different Blender package.')
            self.paths, self.raw, self.joins = session['paths'], session['raw'], session['joins']
            self.applied_settings = session['settings']
            self.hidden = {tuple(p) for p in session['hidden']}
            self.selected = None
            for key, value in session['settings'].items():
                self.variables[key].set(value)
            for group, style in session['styles'].items():
                for var, key in zip(self.styles[group], ('enabled', 'width', 'opacity')):
                    var.set(style[key])
            self.draw()
        except Exception as exc:
            messagebox.showerror('Cannot load session', str(exc))


if __name__ == '__main__':
    root = tk.Tk()
    App(root)
    root.mainloop()
