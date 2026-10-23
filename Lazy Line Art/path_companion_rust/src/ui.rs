use crate::{
    export,
    model::*,
    paths::{self, Processed, Processor},
};
use eframe::egui::{self, Color32, Pos2, Rect, Sense, Stroke as PaintStroke, Vec2};
use rayon::prelude::*;
use std::{
    collections::{HashMap, HashSet},
    sync::{Arc, mpsc},
    time::{Duration, Instant},
};

struct Request {
    serial: u64,
    settings: Settings,
    layers: HashMap<u32, Layer>,
}
struct Reply {
    serial: u64,
    settings: Settings,
    result: Processed,
}
pub struct App {
    package: Option<Arc<Package>>,
    settings: Settings,
    applied: Settings,
    raw: Vec<Stroke>,
    preview: Vec<Stroke>,
    preview_key: Vec<u32>,
    render_key: Vec<u64>,
    render_shapes: Vec<egui::Shape>,
    paths: Vec<Stroke>,
    joins: Vec<[Point; 2]>,
    styles: [Style; 4],
    collection_styles: HashMap<u32, Style>,
    layers: HashMap<u32, Layer>,
    solo: bool,
    preferences: crate::preferences::Preferences,
    preset_name: String,
    thumbnails: HashMap<u32, egui::TextureHandle>,
    hidden: HashSet<usize>,
    selected: Option<usize>,
    collection: Option<u32>,
    background: bool,
    ids: bool,
    original: bool,
    show_joins: bool,
    texture: Option<egui::TextureHandle>,
    colour_texture: Option<egui::TextureHandle>,
    colour_preview: Option<crate::colour::Preview>,
    colour_key: Option<(HashMap<u32, crate::colour::Grade>, bool)>,
    gradient_tool: bool,
    gradient_drop: bool,
    gradient_handle: Option<bool>,
    id_texture: Option<egui::TextureHandle>,
    zoom: f32,
    offset: Vec2,
    fit: bool,
    tx: Option<mpsc::Sender<Request>>,
    rx: Option<mpsc::Receiver<Reply>>,
    serial: u64,
    pending: Option<Instant>,
    busy: bool,
    status: String,
    smoke: bool,
    colour_smoke: bool,
    smoke_frames: u32,
    smoke_cpu: Vec<f32>,
}
impl App {
    pub fn new(
        cc: &eframe::CreationContext<'_>,
        package: Option<Arc<Package>>,
        smoke: bool,
        variation: bool,
        colour_smoke: bool,
    ) -> Self {
        cc.egui_ctx.set_visuals(egui::Visuals::light());
        cc.egui_ctx.tessellation_options_mut(|o| {
            o.feathering = true;
            o.round_line_segments_to_pixels = false;
        });
        let mut app = Self {
            package: None,
            settings: Default::default(),
            applied: Default::default(),
            raw: vec![],
            preview: vec![],
            preview_key: vec![],
            render_key: vec![],
            render_shapes: vec![],
            paths: vec![],
            joins: vec![],
            styles: std::array::from_fn(|_| Style::default()),
            collection_styles: HashMap::new(),
            layers: HashMap::new(),
            solo: false,
            preferences: Default::default(),
            preset_name: String::new(),
            thumbnails: HashMap::new(),
            hidden: HashSet::new(),
            selected: None,
            collection: None,
            background: true,
            ids: false,
            original: false,
            show_joins: true,
            texture: None,
            colour_texture: None,
            colour_preview: None,
            colour_key: None,
            gradient_tool: false,
            gradient_drop: false,
            gradient_handle: None,
            id_texture: None,
            zoom: 1.,
            offset: Vec2::ZERO,
            fit: true,
            tx: None,
            rx: None,
            serial: 0,
            pending: None,
            busy: false,
            status:
                "Open a Blender package to begin. Old packages work without collection controls."
                    .into(),
            smoke,
            colour_smoke,
            smoke_frames: 0,
            smoke_cpu: vec![],
        };
        match crate::preferences::Preferences::load(&crate::preferences::file()) {
            Ok(prefs) => app.preferences = prefs,
            Err(e) => app.status = format!("Could not load saved defaults: {e}"),
        }
        if let Some(package) = package {
            app.install(package, &cc.egui_ctx);
        }
        if variation {
            app.settings.wobble = 2.;
            app.settings.secondary = 0.8;
            app.settings.size_jitter = 65.;
            app.schedule();
        }
        if colour_smoke && let Some(package) = &app.package {
            let id = package
                .manifest
                .collections
                .iter()
                .find(|c| package.ids.contains(&c.id))
                .map_or(0, |c| c.id);
            app.collection = Some(id);
            app.layers.insert(
                id,
                Layer {
                    color: crate::colour::Grade {
                        luminosity: 8.,
                        saturation: 0.6,
                        hue: 15.,
                        contrast: 1.1,
                        gradient: Some(crate::colour::Gradient {
                            start: [0.2, 0.2],
                            end: [0.8, 0.8],
                            amount: -15.,
                        }),
                    },
                    settings: app.settings,
                    styles: app.styles.clone(),
                },
            );
            app.gradient_tool = true;
        }
        app
    }
    fn install(&mut self, package: Arc<Package>, ctx: &egui::Context) {
        let texture = |name: &str, image: &image::RgbaImage, options| {
            ctx.load_texture(
                name,
                egui::ColorImage::from_rgba_unmultiplied(
                    [image.width() as usize, image.height() as usize],
                    image.as_raw(),
                ),
                options,
            )
        };
        self.colour_preview = Some(crate::colour::Preview::new(&package));
        self.colour_key = None;
        self.colour_texture = None;
        self.gradient_tool = false;
        self.gradient_drop = false;
        self.gradient_handle = None;
        self.texture = Some(texture(
            "colour render",
            &package.background,
            egui::TextureOptions::LINEAR,
        ));
        self.id_texture = package
            .id_image
            .as_ref()
            .map(|im| texture("collection ids", im, egui::TextureOptions::NEAREST));
        let (tx, requests) = mpsc::channel::<Request>();
        let (replies, rx) = mpsc::channel();
        let data = package.clone();
        let context = ctx.clone();
        std::thread::spawn(move || {
            let mut processor = Processor::default();
            while let Ok(mut request) = requests.recv() {
                while let Ok(latest) = requests.try_recv() {
                    request = latest;
                }
                let result = processor.process_layers(&data, request.settings, &request.layers);
                if replies
                    .send(Reply {
                        serial: request.serial,
                        settings: request.settings,
                        result,
                    })
                    .is_err()
                {
                    break;
                }
                context.request_repaint();
            }
        });
        self.thumbnails.clear();
        let mut ids: Vec<_> = package
            .manifest
            .collections
            .iter()
            .map(|c| (c.id, c.color))
            .collect();
        ids.push((0, [130, 130, 130]));
        for (id, color) in ids {
            let mut pixels = vec![0u8; 64 * 48 * 4];
            for y in 0..48 {
                for x in 0..64 {
                    let ix = x * package.manifest.width as usize / 64;
                    let iy = y * package.manifest.height as usize / 48;
                    if package.ids[iy * package.manifest.width as usize + ix] == id {
                        pixels[(y * 64 + x) * 4..(y * 64 + x) * 4 + 4]
                            .copy_from_slice(&[color[0], color[1], color[2], 255]);
                    }
                }
            }
            self.thumbnails.insert(
                id,
                ctx.load_texture(
                    format!("layer-{id}"),
                    egui::ColorImage::from_rgba_unmultiplied([64, 48], &pixels),
                    egui::TextureOptions::LINEAR,
                ),
            );
        }
        self.package = Some(package);
        self.tx = Some(tx);
        self.rx = Some(rx);
        self.raw.clear();
        self.preview_key.clear();
        self.paths.clear();
        self.hidden.clear();
        self.selected = None;
        self.collection = self
            .package
            .as_ref()
            .unwrap()
            .manifest
            .collections
            .first()
            .map(|c| c.id)
            .or(Some(0));
        self.solo = false;
        self.collection_styles.clear();
        self.fit = true;
        self.settings = self.preferences.variation;
        self.styles = self.preferences.styles.clone();
        self.layers.clear();
        self.applied = self.settings;
        self.schedule();
    }
    fn schedule(&mut self) {
        self.serial += 1;
        // Throttle, don't debounce: keep producing updates during a continuous drag.
        self.pending.get_or_insert_with(Instant::now);
        self.busy = true;
    }
    fn poll(&mut self, ctx: &egui::Context) {
        if self
            .pending
            .is_some_and(|t| t.elapsed() > Duration::from_millis(45))
        {
            self.pending = None;
            if let Some(tx) = &self.tx {
                let _ = tx.send(Request {
                    serial: self.serial,
                    settings: self.settings,
                    layers: self.layers.clone(),
                });
            }
        }
        if let Some(rx) = &self.rx {
            while let Ok(reply) = rx.try_recv() {
                if reply.serial != self.serial {
                    continue;
                }
                let r = reply.result;
                self.applied = reply.settings;
                self.raw = r.raw;
                self.preview_key.clear();
                self.hidden = remap_hidden(&self.paths, &r.paths, &self.hidden);
                self.paths = r.paths;
                self.joins = r.joins;
                self.busy = false;
                self.status = format!(
                    "{} paths · {} points · processing {:0.1} ms · joins are inferred",
                    self.paths.len(),
                    self.paths.iter().map(|p| p.points.len()).sum::<usize>(),
                    r.milliseconds
                );
            }
        }
        if self.busy {
            ctx.request_repaint_after(Duration::from_millis(16));
        }
    }
    fn session(&self) -> Session {
        Session {
            version: 4,
            fingerprint: self.package.as_ref().unwrap().fingerprint.clone(),
            settings: self.applied,
            styles: self.styles.clone(),
            collection_styles: self.collection_styles.clone(),
            layers: self.layers.clone(),
            hidden: self.hidden.clone(),
            paths: self.paths.clone(),
            raw: self.raw.clone(),
            joins: self.joins.clone(),
        }
    }
    fn save_preferences(&mut self) {
        self.status = match self.preferences.save(&crate::preferences::file()) {
            Ok(()) => "Saved defaults/presets in the app's settings folder. New packages use these defaults.".into(),
            Err(e) => format!("Could not save defaults: {e}"),
        };
    }
    fn sidebar(&mut self, ui: &mut egui::Ui) {
        ui.heading("Path Companion");
        ui.label("Rust · antialiased canvas");
        ui.separator();
        if ui.button("Open Blender package…").clicked()
            && let Some(folder) = rfd::FileDialog::new().pick_folder()
        {
            match Package::load(&folder) {
                Ok(package) => self.install(Arc::new(package), ui.ctx()),
                Err(e) => self.status = format!("Open failed: {e:#}"),
            }
        }
        if self.package.is_none() {
            return;
        }
        ui.separator();
        ui.strong("Layers");
        let package = self.package.as_ref().unwrap();
        let mut rows: Vec<_> = package
            .manifest
            .collections
            .iter()
            .map(|c| (c.id, c.name.clone(), c.path.clone()))
            .collect();
        if package.id_image.is_none() || self.raw.iter().any(|p| p.collection == 0) {
            rows.push((0, "Unassigned".into(), "Unassigned / legacy paths".into()));
        }
        egui::ScrollArea::vertical()
            .id_salt("layer-list")
            .max_height(220.)
            .show(ui, |ui| {
                for (id, name, path) in &rows {
                    ui.horizontal(|ui| {
                        let style = self.collection_styles.entry(*id).or_insert(Style {
                            width: 1.,
                            ..Default::default()
                        });
                        ui.checkbox(&mut style.visible, "")
                            .on_hover_text("Show layer linework");
                        if let Some(texture) = self.thumbnails.get(id) {
                            let image = egui::Image::new((texture.id(), Vec2::new(40., 30.)))
                                .sense(Sense::click());
                            if ui.add(image).clicked() {
                                self.collection = Some(*id);
                            }
                        }
                        if ui
                            .selectable_label(self.collection == Some(*id), name)
                            .on_hover_text(path)
                            .clicked()
                        {
                            self.collection = Some(*id);
                        }
                    });
                }
            });
        ui.checkbox(&mut self.solo, "Solo selected layer");
        ui.small("Visibility controls linework; the colour render is a reference.");
        let id = self.collection.unwrap_or(0);
        let name = rows
            .iter()
            .find(|(i, _, _)| *i == id)
            .map(|(_, n, _)| n.as_str())
            .unwrap_or("Unassigned");
        ui.separator();
        ui.strong(format!("Settings: {name}"));
        let mut layer = self.layers.get(&id).cloned().unwrap_or(Layer {
            color: Default::default(),
            settings: self.settings,
            styles: self.styles.clone(),
        });
        let before = layer.clone();
        ui.menu_button("Copy from layer", |ui| {
            for (source, name, _) in &rows {
                if *source != id && ui.button(name).clicked() {
                    layer = self.layers.get(source).cloned().unwrap_or(Layer {
                        color: Default::default(),
                        settings: self.settings,
                        styles: self.styles.clone(),
                    });
                    let style = self
                        .collection_styles
                        .get(source)
                        .cloned()
                        .unwrap_or(Style {
                            width: 1.,
                            ..Default::default()
                        });
                    self.collection_styles.insert(id, style);
                    ui.close();
                }
            }
        });
        let style = self.collection_styles.entry(id).or_insert(Style {
            width: 1.,
            ..Default::default()
        });
        ui.add(egui::Slider::new(&mut style.opacity, 0.0..=1.).text("Layer opacity"));
        ui.add(egui::Slider::new(&mut style.width, 0.01..=5.).text("Width multiplier"));
        egui::CollapsingHeader::new("Colour & light").default_open(true).show(ui, |ui| {
            ui.add(egui::Slider::new(&mut layer.color.luminosity,-100.0..=100.).text("Luminosity"));
            ui.add(egui::Slider::new(&mut layer.color.saturation,0.0..=2.).text("Saturation"));
            ui.add(egui::Slider::new(&mut layer.color.hue,-180.0..=180.).text("Hue · degrees"));
            ui.add(egui::Slider::new(&mut layer.color.contrast,0.0..=2.).text("Contrast"));
            if ui.small_button("Reset colour & gradient").clicked() { layer.color=Default::default(); }
            ui.separator();
            let drop=ui.add(egui::Button::new("Drag gradient onto image").sense(Sense::click_and_drag()));
            if drop.drag_started() { self.gradient_drop=true; self.gradient_tool=true; self.background=true; self.ids=false; }
            if drop.clicked() { self.gradient_tool=true; self.background=true; self.ids=false; }
            ui.checkbox(&mut self.gradient_tool,"Edit gradient on canvas");
            if let Some(g)=&mut layer.color.gradient {
                ui.add(egui::Slider::new(&mut g.amount,-100.0..=100.).text("Gradient light"));
                ui.horizontal(|ui| {
                    if ui.small_button("Reverse").clicked() { std::mem::swap(&mut g.start,&mut g.end); }
                    if ui.small_button("Darken").clicked() { g.amount = -g.amount.abs(); }
                    if ui.small_button("Lighten").clicked() { g.amount=g.amount.abs(); }
                });
                if ui.small_button("Remove gradient").clicked() { layer.color.gradient=None; }
            }
            ui.small("Drag on the image to draw a new ramp; drag either handle to reshape it. One gradient per layer; a new one replaces it.");
        });
        egui::CollapsingHeader::new("Reconstruction")
            .default_open(true)
            .show(ui, |ui| {
                ui.add(
                    egui::Slider::new(&mut layer.settings.threshold, 0.001..=0.999)
                        .text("Mask threshold"),
                );
                ui.add(egui::Slider::new(&mut layer.settings.gap, 0.0..=20.).text("Join gap · px"));
                ui.add(
                    egui::Slider::new(&mut layer.settings.tolerance, 0.0..=5.)
                        .text("Simplify · px"),
                );
                ui.add(
                    egui::Slider::new(&mut layer.settings.minimum, 0.0..=1000.)
                        .text("Min length · px"),
                );
            });
        egui::CollapsingHeader::new("Line variation").default_open(false).show(ui, |ui| {
            ui.horizontal(|ui| {
                ui.menu_button("Presets", |ui| {
                    if ui.button("Subtle - your preset").clicked() { layer.settings.copy_variation(Settings::subtle()); ui.close(); }
                    if ui.button("No variation").clicked() { layer.settings.copy_variation(Settings::default()); ui.close(); }
                    if ui.button("Saved default").clicked() { layer.settings.copy_variation(self.preferences.variation); ui.close(); }
                    for (name, preset) in &self.preferences.presets {
                        if ui.button(name).clicked() { layer.settings.copy_variation(*preset); ui.close(); }
                    }
                });
                if ui.small_button("Save as default").clicked() {
                    self.preferences.variation.copy_variation(layer.settings);
                    self.save_preferences();
                }
            });
            ui.horizontal(|ui| {
                ui.add(egui::TextEdit::singleline(&mut self.preset_name).hint_text("Preset name").desired_width(130.));
                if ui.add_enabled(!self.preset_name.trim().is_empty(), egui::Button::new("Save preset")).clicked() {
                    let name = self.preset_name.trim().to_owned();
                    self.preferences.presets.retain(|(n, _)| n != &name);
                    self.preferences.presets.push((name, layer.settings));
                    self.save_preferences();
                }
            });
            ui.strong("Primary wobble");
            ui.add(egui::Slider::new(&mut layer.settings.wobble, 0.0..=20.).text("Amplitude · px"));
            ui.add(egui::Slider::new(&mut layer.settings.wavelength, 2.0..=500.).text("Wavelength · px"));
            ui.strong("Secondary wobble");
            ui.add(egui::Slider::new(&mut layer.settings.secondary, 0.0..=20.).text("Amplitude · px"));
            ui.add(egui::Slider::new(&mut layer.settings.secondary_wavelength, 2.0..=500.).text("Wavelength · px"));
            if ui.small_button("Set to half primary wavelength").clicked() {
                layer.settings.secondary_wavelength = (layer.settings.wavelength / 2.).max(2.);
            }
            ui.strong("Thickness jitter");
            ui.add(egui::Slider::new(&mut layer.settings.size_jitter, 0.0..=100.).text("Amplitude · %"));
            ui.add(egui::Slider::new(&mut layer.settings.size_wavelength, 2.0..=500.).text("Wavelength · px"));
            ui.horizontal(|ui| {
                ui.label("Seed");
                ui.add(egui::DragValue::new(&mut layer.settings.seed));
            });
            ui.small("Smooth, repeatable variation. Wobble endpoints stay pinned. Thickness is relative to each line’s width.");
        });

        ui.horizontal(|ui| {
            ui.strong("Line groups");
            if ui.small_button("Save as default").clicked() {
                self.preferences.styles = layer.styles.clone();
                self.save_preferences();
            }
            if ui.small_button("Use default").clicked() {
                layer.styles = self.preferences.styles.clone();
            }
        });
        egui::Grid::new("line-groups").striped(true).show(ui, |ui| {
            ui.label("Show / group");
            ui.label("Width");
            ui.label("Opacity");
            ui.end_row();
            for (group, name) in GROUPS.iter().enumerate() {
                ui.checkbox(&mut layer.styles[group].visible, *name);
                ui.add(
                    egui::DragValue::new(&mut layer.styles[group].width)
                        .speed(0.1)
                        .range(0.01..=100.),
                );
                ui.add(
                    egui::DragValue::new(&mut layer.styles[group].opacity)
                        .speed(0.01)
                        .range(0.0..=1.),
                );
                ui.end_row();
            }
        });
        if before != layer {
            if before.settings.reconstruction_key() != layer.settings.reconstruction_key() {
                self.hidden
                    .retain(|index| self.paths.get(*index).is_some_and(|p| p.collection != id));
                self.selected = None;
            }
            let processing_changed = before.settings != layer.settings;
            self.layers.insert(id, layer);
            if processing_changed {
                self.schedule();
            }
        }
        ui.separator();
        ui.checkbox(&mut self.background, "Colour render");
        ui.checkbox(&mut self.ids, "Collection ID image");
        ui.checkbox(&mut self.original, "Original paths (before cleanup)");
        ui.checkbox(&mut self.show_joins, "Highlight joins");
        ui.horizontal(|ui| {
            if ui.button("Fit").clicked() {
                self.fit = true;
            }
            if ui.button("100%").clicked() {
                self.zoom = 1. / ui.ctx().pixels_per_point();
                self.offset = Vec2::splat(12.);
            }
        });
        ui.small("Wheel: zoom · right/middle drag: pan · click: select");
        ui.horizontal(|ui| {
            if ui
                .add_enabled(
                    self.selected.is_some() && !self.original,
                    egui::Button::new("Hide selected"),
                )
                .clicked()
            {
                self.hidden.insert(self.selected.take().unwrap());
            }
            if ui.button("Restore hidden").clicked() {
                self.hidden.clear();
            }
        });
        ui.separator();
        if ui
            .add_enabled(!self.busy, egui::Button::new("Save SVG + session…"))
            .clicked()
            && let Some(file) = rfd::FileDialog::new()
                .add_filter("SVG", &["svg"])
                .set_file_name("linework.svg")
                .save_file()
        {
            self.status =
                match export::save_svg(&file, self.package.as_ref().unwrap(), &self.session()) {
                    Ok(()) => "Saved grouped SVG and .paths.json session".into(),
                    Err(e) => format!("Save failed: {e:#}"),
                };
        }
        if ui.button("Save adjusted colour PNG…").clicked()
            && let Some(file) = rfd::FileDialog::new()
                .add_filter("PNG", &["png"])
                .set_file_name("colour-adjusted.png")
                .save_file()
        {
            self.status=match crate::colour::save(&file,self.package.as_ref().unwrap(),&self.layers) {
                Ok(())=>"Saved full-resolution colour PNG with original transparency; linework is exported separately.".into(),
                Err(e)=>format!("Colour export failed: {e:#}"),
            };
        }
        if ui.button("Load Rust session…").clicked()
            && let Some(file) = rfd::FileDialog::new()
                .add_filter("Path session", &["json"])
                .pick_file()
        {
            let loaded = (|| -> anyhow::Result<Session> {
                let s: Session = serde_json::from_slice(&std::fs::read(file)?)?;
                s.validate(self.package.as_ref().unwrap())?;
                Ok(s)
            })();
            match loaded {
                Ok(s) => {
                    self.serial += 1;
                    self.pending = None;
                    self.busy = false;
                    self.settings = s.settings;
                    self.applied = s.settings;
                    self.styles = s.styles;
                    self.collection_styles = s.collection_styles;
                    self.layers = s.layers;
                    self.colour_key = None;
                    self.gradient_handle = None;
                    self.paths = s.paths;
                    self.hidden = s.hidden;
                    self.selected = None;
                    self.original = false;
                    self.raw = s.raw;
                    self.preview_key.clear();
                    self.joins = s.joins;
                    self.status =
                        "Session restored, including collection styles and hidden paths.".into();
                }
                Err(e) => self.status = format!("Load failed: {e:#}"),
            }
        }
        if ui
            .add_enabled(
                self.id_texture.is_some(),
                egui::Button::new("Save Photoshop ID PNG…"),
            )
            .clicked()
            && let Some(folder) = rfd::FileDialog::new().pick_folder()
        {
            self.status=match export::save_ids(&folder,self.package.as_ref().unwrap()){Ok(())=>"Saved collection_ids.png + collection_palette.json. Magic Wand: tolerance 0, contiguous OFF.".into(),Err(e)=>format!("ID export failed: {e:#}")};
        }
    }
    fn canvas(&mut self, ui: &mut egui::Ui) {
        let (rect, response) = ui.allocate_exact_size(ui.available_size(), Sense::click_and_drag());
        let painter = ui.painter_at(rect);
        painter.rect_filled(rect, 0., Color32::from_gray(212));
        let Some(package) = &self.package else {
            return;
        };
        let size = Vec2::new(
            package.manifest.width as f32,
            package.manifest.height as f32,
        );
        if self.fit {
            self.zoom = ((rect.width() - 24.) / size.x)
                .min((rect.height() - 24.) / size.y)
                .max(0.01);
            self.offset = (rect.size() - size * self.zoom) * 0.5;
            self.fit = false;
        }
        if response.hovered() {
            let scroll = ui.input(|i| i.smooth_scroll_delta.y);
            if scroll != 0. {
                let old = self.zoom;
                self.zoom = (old * (scroll * 0.002).exp()).clamp(0.01, 64.);
                if let Some(p) = response.hover_pos() {
                    let cursor = p - rect.min;
                    self.offset = cursor - (cursor - self.offset) * (self.zoom / old);
                }
            }
        }
        if response.dragged_by(egui::PointerButton::Secondary)
            || response.dragged_by(egui::PointerButton::Middle)
        {
            self.offset += ui.input(|i| i.pointer.delta());
        }
        let origin = rect.min + self.offset;
        let image_rect = Rect::from_min_size(origin, size * self.zoom);
        painter.rect_filled(image_rect, 0., Color32::WHITE);
        let active_id = self.collection.unwrap_or(0);
        let cursor = ui.input(|i| i.pointer.interact_pos());
        let normalized = |p: Pos2| {
            [
                ((p.x - origin.x) / (size.x * self.zoom)).clamp(0., 1.),
                ((p.y - origin.y) / (size.y * self.zoom)).clamp(0., 1.),
            ]
        };
        let screen = |p: Point| origin + Vec2::new(p[0] * size.x, p[1] * size.y) * self.zoom;
        if self.gradient_drop && ui.input(|i| i.pointer.any_released()) {
            self.gradient_drop = false;
            if let Some(p) = cursor.filter(|p| image_rect.contains(*p) && rect.contains(*p)) {
                let center = normalized(p);
                let layer = self.layers.entry(active_id).or_insert(Layer {
                    color: Default::default(),
                    settings: self.settings,
                    styles: self.styles.clone(),
                });
                let amount = layer.color.gradient.map_or(20., |g| g.amount);
                layer.color.gradient = Some(crate::colour::Gradient {
                    start: [(center[0] - 0.15).max(0.), center[1]],
                    end: [(center[0] + 0.15).min(1.), center[1]],
                    amount,
                });
            }
        }
        if self.gradient_tool && !self.gradient_drop {
            if response.drag_started_by(egui::PointerButton::Primary)
                && let Some(p) = ui
                    .input(|i| i.pointer.press_origin())
                    .filter(|p| image_rect.contains(*p))
            {
                let layer = self.layers.entry(active_id).or_insert(Layer {
                    color: Default::default(),
                    settings: self.settings,
                    styles: self.styles.clone(),
                });
                let handle = layer.color.gradient.and_then(|g| {
                    if screen(g.start).distance(p) < 12. {
                        Some(true)
                    } else if screen(g.end).distance(p) < 12. {
                        Some(false)
                    } else {
                        None
                    }
                });
                if let Some(handle) = handle {
                    self.gradient_handle = Some(handle);
                } else {
                    let amount = layer.color.gradient.map_or(20., |g| g.amount);
                    layer.color.gradient = Some(crate::colour::Gradient {
                        start: normalized(p),
                        end: normalized(p),
                        amount,
                    });
                    self.gradient_handle = Some(false);
                }
            }
            if response.dragged_by(egui::PointerButton::Primary)
                && let (Some(p), Some(start)) = (cursor, self.gradient_handle)
                && let Some(g) = self
                    .layers
                    .get_mut(&active_id)
                    .and_then(|l| l.color.gradient.as_mut())
            {
                if start {
                    g.start = normalized(p);
                } else {
                    g.end = normalized(p);
                }
            }
        }
        if ui.input(|i| i.pointer.any_released()) {
            self.gradient_handle = None;
        }
        if ui.input(|i| i.key_pressed(egui::Key::Escape)) {
            self.gradient_tool = false;
            self.gradient_drop = false;
            self.gradient_handle = None;
        }
        let grades = crate::colour::grades(&self.layers);
        let changing = self
            .colour_key
            .as_ref()
            .is_none_or(|(old, low)| *old != grades || *low);
        let low = changing && ui.input(|i| i.pointer.any_down());
        if self.colour_key.as_ref() != Some(&(grades.clone(), low)) {
            if grades.is_empty() {
                self.colour_texture = None;
            } else {
                let adjusted = if low {
                    let preview = self.colour_preview.as_ref().unwrap();
                    crate::colour::render(&preview.image, &preview.ids, &grades)
                } else {
                    crate::colour::render(&package.background, &package.ids, &grades)
                };
                let image = egui::ColorImage::from_rgba_unmultiplied(
                    [adjusted.width() as usize, adjusted.height() as usize],
                    adjusted.as_raw(),
                );
                if let Some(texture) = &mut self.colour_texture {
                    texture.set(image, egui::TextureOptions::LINEAR);
                } else {
                    self.colour_texture = Some(ui.ctx().load_texture(
                        "adjusted colour",
                        image,
                        egui::TextureOptions::LINEAR,
                    ));
                }
            }
            self.colour_key = Some((grades, low));
        }
        let texture = if self.ids {
            self.id_texture.as_ref()
        } else if self.background {
            self.colour_texture.as_ref().or(self.texture.as_ref())
        } else {
            None
        };
        if let Some(texture) = texture {
            painter.image(
                texture.id(),
                image_rect,
                Rect::from_min_max(Pos2::ZERO, Pos2::new(1., 1.)),
                Color32::WHITE,
            );
        }
        let painter = painter.with_clip_rect(rect.intersect(image_rect));
        let source = if self.original {
            &self.raw
        } else {
            &self.paths
        };
        let mut key = vec![
            self.original as u32,
            self.zoom.to_bits(),
            ui.ctx().pixels_per_point().to_bits(),
        ];
        key.extend(self.styles.iter().map(|s| s.width.to_bits()));
        for (&id, style) in &self.collection_styles {
            key.extend([id, style.width.to_bits()]);
        }
        key.push(u32::MAX);
        for (&id, layer) in &self.layers {
            key.push(id);
            key.extend(layer.styles.iter().map(|s| s.width.to_bits()));
        }
        if self.preview_key != key {
            let scale = self.zoom * ui.ctx().pixels_per_point();
            self.preview = source
                .par_iter()
                .map(|p| {
                    let style = export::effective_style(
                        p.group,
                        p.collection,
                        &self.styles,
                        &self.collection_styles,
                        &self.layers,
                    );
                    crate::ribbon::preview(p, scale, style.width)
                })
                .collect();
            self.preview_key = key;
            self.render_key.clear();
        }
        let lines = &self.preview;
        let mut closest = (6. / self.zoom, None);
        let hit = if response.clicked() && !self.original && !self.gradient_tool {
            response.interact_pointer_pos().map(|p| {
                let p = (p - origin) / self.zoom;
                [p.x, p.y]
            })
        } else {
            None
        };
        let mut render_key: Vec<u64> = self.preview_key.iter().map(|v| *v as u64).collect();
        render_key.extend([
            origin.x.to_bits() as u64,
            origin.y.to_bits() as u64,
            rect.width().to_bits() as u64,
            rect.height().to_bits() as u64,
            self.selected.unwrap_or(usize::MAX) as u64,
            self.solo as u64,
            self.collection.unwrap_or(u32::MAX) as u64,
        ]);
        render_key.extend(self.hidden.iter().map(|v| *v as u64));
        render_key.push(u64::MAX);
        for style in &self.styles {
            render_key.extend([style.visible as u64, style.opacity.to_bits() as u64]);
        }
        for (&id, style) in &self.collection_styles {
            render_key.extend([
                id as u64,
                style.visible as u64,
                style.opacity.to_bits() as u64,
            ]);
        }
        for (&id, layer) in &self.layers {
            render_key.push(id as u64);
            for style in &layer.styles {
                render_key.extend([style.visible as u64, style.opacity.to_bits() as u64]);
            }
        }
        let redraw = self.render_key != render_key;
        if redraw {
            self.render_shapes.clear();
        }
        if redraw || hit.is_some() {
            for (index, line) in lines.iter().enumerate() {
                if (!self.original && self.hidden.contains(&index))
                    || (self.solo && self.collection.is_some_and(|c| line.collection != c))
                {
                    continue;
                }
                let style = export::effective_style(
                    line.group,
                    line.collection,
                    &self.styles,
                    &self.collection_styles,
                    &self.layers,
                );
                if !style.visible || style.opacity <= 0. {
                    continue;
                }
                let positions: Vec<Pos2> = line
                    .points
                    .iter()
                    .map(|p| origin + Vec2::new(p[0], p[1]) * self.zoom)
                    .collect();
                let bounds = Rect::from_points(&positions);
                if !bounds.expand(style.width * self.zoom).intersects(rect) {
                    continue;
                }
                let color = if self.selected == Some(index) && !self.original {
                    Color32::from_rgb(0, 140, 200)
                } else {
                    Color32::from_rgba_unmultiplied(
                        23,
                        23,
                        23,
                        (style.opacity * 255.).round() as u8,
                    )
                };
                if redraw {
                    // No minimum pixel-width clamp: egui feathering represents subpixel strokes by coverage.
                    if line.widths.is_empty() {
                        self.render_shapes.push(egui::Shape::line(
                            positions,
                            PaintStroke::new(style.width * self.zoom, color),
                        ));
                    } else {
                        self.render_shapes.push(crate::ribbon::mesh(
                            &positions,
                            &line.widths,
                            style.width * self.zoom,
                            color,
                            1. / ui.ctx().pixels_per_point(),
                        ));
                    }
                }
                if let Some(p) = hit {
                    for pair in line.points.windows(2) {
                        let d = paths::segment_distance(p, pair[0], pair[1]);
                        if d < closest.0 {
                            closest = (d, Some(index));
                        }
                    }
                }
            }
        }
        self.render_key = render_key;
        painter.extend(self.render_shapes.iter().cloned());
        if hit.is_some() {
            self.selected = closest.1;
        }
        if self.show_joins && !self.original {
            for join in &self.joins {
                let id = package.collection_at(join[0]);
                if (self.solo && self.collection != Some(id))
                    || !export::effective_style(
                        0,
                        id,
                        &self.styles,
                        &self.collection_styles,
                        &self.layers,
                    )
                    .visible
                {
                    continue;
                }
                painter.line_segment(
                    join.map(|p| origin + Vec2::new(p[0], p[1]) * self.zoom),
                    PaintStroke::new(2., Color32::from_rgb(235, 130, 0)),
                );
            }
        }
        if self.gradient_tool
            && let Some(g) = self.layers.get(&active_id).and_then(|l| l.color.gradient)
        {
            let a = screen(g.start);
            let b = screen(g.end);
            painter.line_segment(
                [a, b],
                PaintStroke::new(2., Color32::from_rgb(40, 150, 230)),
            );
            for point in [a, b] {
                painter.circle_filled(point, 6., Color32::WHITE);
                painter.circle_stroke(
                    point,
                    6.,
                    PaintStroke::new(2., Color32::from_rgb(40, 150, 230)),
                );
            }
        }
    }
}
impl eframe::App for App {
    fn ui(&mut self, ui: &mut egui::Ui, frame: &mut eframe::Frame) {
        let ctx = ui.ctx().clone();
        self.poll(&ctx);
        egui::Panel::bottom("status").show(ui, |ui| {
            ui.horizontal_wrapped(|ui| {
                if self.busy {
                    ui.spinner();
                }
                ui.label(&self.status);
            });
        });
        egui::Panel::left("controls")
            .default_size(310.)
            .resizable(true)
            .show(ui, |ui| {
                egui::ScrollArea::vertical().show(ui, |ui| self.sidebar(ui));
            });
        egui::CentralPanel::default().show(ui, |ui| self.canvas(ui));
        if self.smoke && !self.busy && !self.paths.is_empty() {
            self.smoke_frames += 1;
            if self.smoke_frames > 3
                && let Some(cpu) = frame.info().cpu_usage
            {
                self.smoke_cpu.push(cpu * 1000.);
            }
            if self.smoke_frames == 12 {
                ctx.send_viewport_cmd(egui::ViewportCommand::Screenshot(Default::default()));
            }
            for event in ctx.input(|i| i.events.clone()) {
                if let egui::Event::Screenshot { image, .. } = event {
                    let raw: Vec<u8> = image.pixels.iter().flat_map(|p| p.to_array()).collect();
                    let file = self
                        .package
                        .as_ref()
                        .unwrap()
                        .folder
                        .join("rust_preview.png");
                    if let Err(e) = image::save_buffer(
                        &file,
                        &raw,
                        image.size[0] as u32,
                        image.size[1] as u32,
                        image::ColorType::Rgba8,
                    ) {
                        eprintln!("Screenshot failed: {e}");
                    }
                    println!("UI smoke screenshot: {}", file.display());
                    if self.colour_smoke {
                        let package = self.package.as_ref().unwrap();
                        match crate::colour::save(
                            &package.folder.join("colour_test.png"),
                            package,
                            &self.layers,
                        ) {
                            Ok(()) => println!("Full-resolution colour PNG exported."),
                            Err(e) => eprintln!("Colour export failed: {e}"),
                        }
                        let session = self.session();
                        let saved = serde_json::to_vec(&session).unwrap();
                        let restored: Session = serde_json::from_slice(&saved).unwrap();
                        restored.validate(package).unwrap();
                        assert_eq!(restored.layers, session.layers);
                        println!("Colour and gradient session roundtrip verified.");
                    }
                    if !self.smoke_cpu.is_empty() {
                        self.smoke_cpu.sort_by(f32::total_cmp);
                        println!(
                            "UI frame CPU median: {:.2} ms (not GPU/presentation time)",
                            self.smoke_cpu[self.smoke_cpu.len() / 2]
                        );
                    }
                    ctx.send_viewport_cmd(egui::ViewportCommand::Close);
                }
            }
            ctx.request_repaint_after(Duration::from_millis(100));
            if self.smoke_frames > 100 {
                ctx.send_viewport_cmd(egui::ViewportCommand::Close);
            }
        }
    }
}

fn remap_hidden(old: &[Stroke], new: &[Stroke], hidden: &HashSet<usize>) -> HashSet<usize> {
    let keys = |lines: &[Stroke]| {
        let mut counts = HashMap::<(u32, usize), usize>::new();
        lines
            .iter()
            .map(|p| {
                let count = counts.entry((p.collection, p.group)).or_default();
                let key = (p.collection, p.group, *count);
                *count += 1;
                key
            })
            .collect::<Vec<_>>()
    };
    let old_keys = keys(old);
    let retained: HashSet<_> = hidden
        .iter()
        .filter_map(|i| old_keys.get(*i).copied())
        .collect();
    keys(new)
        .iter()
        .enumerate()
        .filter_map(|(i, k)| retained.contains(k).then_some(i))
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn hidden_paths_survive_other_layer_removal() {
        let stroke = |collection| Stroke {
            points: vec![[0., 0.], [1., 1.]],
            widths: vec![],
            group: 0,
            collection,
        };
        let old = vec![stroke(1), stroke(1), stroke(2), stroke(2)];
        let new = vec![stroke(2), stroke(2)];
        assert_eq!(
            remap_hidden(&old, &new, &HashSet::from([3])),
            HashSet::from([1])
        );
    }
    #[test]
    fn subpixel_strokes_keep_distinct_coverage() {
        let ctx = egui::Context::default();
        ctx.begin_pass(Default::default());
        let _ = ctx.end_pass();
        let alpha = |width| {
            let shapes = vec![egui::epaint::ClippedShape {
                clip_rect: Rect::EVERYTHING,
                shape: egui::Shape::line(
                    vec![Pos2::new(10., 10.), Pos2::new(100., 10.)],
                    PaintStroke::new(width, Color32::BLACK),
                ),
            }];
            ctx.tessellate(shapes, 1.)
                .iter()
                .filter_map(|p| match &p.primitive {
                    egui::epaint::Primitive::Mesh(mesh) => {
                        mesh.vertices.iter().map(|v| v.color.a()).max()
                    }
                    _ => None,
                })
                .max()
                .unwrap()
        };
        assert!(alpha(0.2) > 0);
        assert!(alpha(0.2) < alpha(0.8));
        assert!(alpha(0.8) < alpha(2.));
    }
}
