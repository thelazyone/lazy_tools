use anyhow::{Context, Result, bail, ensure};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    collections::HashMap,
    path::{Path, PathBuf},
};

pub type Point = [f32; 2];
pub const GROUPS: [&str; 4] = ["Geometry", "Shadow", "Hard shadow", "Grunge"];

#[derive(Clone, Deserialize, Serialize, Debug)]
pub struct Collection {
    pub id: u32,
    pub name: String,
    pub path: String,
    pub color: [u8; 3],
}
#[derive(Clone, Deserialize)]
pub struct Geometry {
    // Retain export precision until off-screen segments have been clipped.
    pub points: Vec<[f64; 2]>,
}
#[derive(Deserialize)]
pub struct Manifest {
    pub version: u32,
    pub width: u32,
    pub height: u32,
    pub files: HashMap<String, String>,
    #[serde(default)]
    pub geometry: Vec<Geometry>,
    #[serde(default)]
    pub collections: Vec<Collection>,
    #[serde(default)]
    pub mask_encoding: String,
}
pub struct Package {
    pub manifest: Manifest,
    pub folder: PathBuf,
    pub fingerprint: String,
    pub background: image::RgbaImage,
    pub id_image: Option<image::RgbaImage>,
    pub ids: Vec<u32>,
    pub masks: [Vec<f32>; 3],
}
impl Package {
    pub fn load(folder: &Path) -> Result<Self> {
        let folder = folder.canonicalize()?;
        let bytes = std::fs::read(folder.join("package.json"))?;
        let mut manifest: Manifest = serde_json::from_slice(&bytes)?;
        ensure!(manifest.version == 1, "Unsupported Blender package version");
        ensure!(
            manifest.width >= 2 && manifest.height >= 2,
            "Image is too small"
        );
        ensure!(
            manifest.width as u64 * manifest.height as u64 <= 100_000_000,
            "Package exceeds 100 megapixels"
        );
        for (stroke, line) in manifest.geometry.iter().enumerate() {
            ensure!(
                line.points.iter().all(|p| p.iter().all(|v| v.is_finite())),
                "Stroke {stroke} has non-finite geometry coordinates"
            );
        }
        // Large projections are valid for objects near the camera or outside
        // its frame. Do not reject the package or clamp points onto the border.
        // Include the maximum supported half-width (100 * 100 / 2), wobble
        // (40px), up to 2x width jitter, and a small coverage margin.
        let margin = 10044.;
        let bounds = [
            -margin,
            -margin,
            manifest.width as f64 + margin,
            manifest.height as f64 + margin,
        ];
        manifest.geometry = manifest
            .geometry
            .iter()
            .flat_map(|line| clip_polyline(&line.points, bounds))
            .map(|points| Geometry { points })
            .collect();
        ensure!(
            manifest.geometry.iter().flat_map(|s| &s.points).all(|p| p
                .iter()
                .all(|v| v.is_finite())
                && p[0] >= bounds[0] - 1.
                && p[0] <= bounds[2] + 1.
                && p[1] >= bounds[1] - 1.
                && p[1] <= bounds[3] + 1.),
            "Geometry projection exceeded numerical precision while clipping; re-export with a different camera position"
        );
        let read = |key: &str| -> Result<image::RgbaImage> {
            let relative = manifest
                .files
                .get(key)
                .with_context(|| format!("Missing {key}"))?;
            let file = folder.join(relative).canonicalize()?;
            ensure!(file.starts_with(&folder), "Image is outside package folder");
            let im = image::open(file)?.to_rgba8();
            ensure!(
                im.dimensions() == (manifest.width, manifest.height),
                "Wrong dimensions for {key}"
            );
            Ok(im)
        };
        let background = read("flats")?;
        let srgb = manifest.mask_encoding == "srgb";
        let decode = |key: &str| -> Result<Vec<f32>> {
            Ok(read(key)?
                .pixels()
                .map(|p| {
                    let x = p[0] as f32 / 255.;
                    if !srgb {
                        x
                    } else if x <= 0.04045 {
                        x / 12.92
                    } else {
                        ((x + 0.055) / 1.055).powf(2.4)
                    }
                })
                .collect())
        };
        let masks = [
            decode("shadow_mask")?,
            decode("hard_shadow_mask")?,
            decode("grunge_mask")?,
        ];
        let id_image = if manifest.files.contains_key("collection_ids") {
            Some(read("collection_ids")?)
        } else {
            None
        };
        let palette: HashMap<[u8; 3], u32> = manifest
            .collections
            .iter()
            .map(|c| (c.color, c.id))
            .collect();
        let ids = if let Some(im) = &id_image {
            let mut ids = Vec::with_capacity(im.len() / 4);
            for p in im.pixels() {
                let color = [p[0], p[1], p[2]];
                if color == [0, 0, 0] {
                    ids.push(0)
                } else if let Some(id) = palette.get(&color) {
                    ids.push(*id)
                } else {
                    bail!("Collection ID image contains a colour missing from the palette")
                }
            }
            ids
        } else {
            vec![0; (manifest.width * manifest.height) as usize]
        };
        Ok(Self {
            manifest,
            folder,
            fingerprint: format!("{:x}", Sha256::digest(bytes)),
            background,
            id_image,
            ids,
            masks,
        })
    }
    pub fn collection_at(&self, p: Point) -> u32 {
        let x = p[0].floor() as i32;
        let y = p[1].floor() as i32;
        let w = self.manifest.width as i32;
        let h = self.manifest.height as i32;
        let sample = |x: i32, y: i32| {
            if x >= 0 && y >= 0 && x < w && y < h {
                self.ids[(y * w + x) as usize]
            } else {
                0
            }
        };
        let id = sample(x, y);
        if id != 0 {
            return id;
        }
        // Silhouette centre lines can sit just outside the surface coverage.
        for radius in 1i32..=2 {
            let mut best = (i32::MAX, 0);
            for dy in -radius..=radius {
                for dx in -radius..=radius {
                    let id = sample(x + dx, y + dy);
                    let distance = dx * dx + dy * dy;
                    if id != 0 && (distance, id) < best {
                        best = (distance, id);
                    }
                }
            }
            if best.1 != 0 {
                return best.1;
            }
        }
        0
    }
}

/// Liang–Barsky segment clipping; split disconnected visible runs instead of
/// joining an exit to the next re-entry along the image border.
fn clip_polyline(points: &[[f64; 2]], bounds: [f64; 4]) -> Vec<Vec<[f64; 2]>> {
    let mut result = Vec::new();
    let mut run: Vec<[f64; 2]> = Vec::new();
    for pair in points.windows(2) {
        let a = pair[0];
        let b = pair[1];
        let d = [b[0] - a[0], b[1] - a[1]];
        let mut enter: f64 = 0.;
        let mut exit: f64 = 1.;
        let mut visible = true;
        for (p, q) in [
            (-d[0], a[0] - bounds[0]),
            (d[0], bounds[2] - a[0]),
            (-d[1], a[1] - bounds[1]),
            (d[1], bounds[3] - a[1]),
        ] {
            if p == 0. {
                if q < 0. {
                    visible = false;
                    break;
                }
            } else if p < 0. {
                enter = enter.max(q / p);
            } else {
                exit = exit.min(q / p);
            }
        }
        if !visible || enter > exit {
            if run.len() >= 2 {
                result.push(std::mem::take(&mut run));
            }
            run.clear();
            continue;
        }
        let at = |t| [a[0] + d[0] * t, a[1] + d[1] * t];
        let first = if enter == 0. { a } else { at(enter) };
        let last = if exit == 1. { b } else { at(exit) };
        if run.last().is_some_and(|p| *p != first) {
            if run.len() >= 2 {
                result.push(std::mem::take(&mut run));
            }
            run.clear();
        }
        if run.is_empty() {
            run.push(first);
        }
        if run.last() != Some(&last) {
            run.push(last);
        }
        if exit < 1. {
            if run.len() >= 2 {
                result.push(std::mem::take(&mut run));
            }
            run.clear();
        }
    }
    if run.len() >= 2 {
        result.push(run);
    }
    if points.first() == points.last()
        && result.len() > 1
        && result.last().unwrap().last() == result[0].first()
    {
        let mut last = result.pop().unwrap();
        last.extend_from_slice(&result[0][1..]);
        result[0] = last;
    }
    result
}

#[cfg(test)]
mod clipping_tests {
    use super::*;
    #[test]
    fn distant_collection_stroke_does_not_reject_or_create_border_line() {
        let p = [
            [9904532.34375, -2174529.0454101562],
            [10290564.140625, -2174461.4135742188],
        ];
        assert!(clip_polyline(&p, [-5024., -5024., 10784., 8264.]).is_empty());
    }
    #[test]
    fn huge_crossing_retains_visible_segment() {
        let p = [[-1e8, 50.], [1e8, 50.]];
        let result = clip_polyline(&p, [0., 0., 100., 100.]);
        assert_eq!(result.len(), 1);
        assert!((result[0][0][0]).abs() < 1e-6);
        assert!((result[0][1][0] - 100.).abs() < 1e-6);
        assert_eq!(result[0][0][1], 50.);
    }
    #[test]
    fn exit_and_reentry_are_not_connected() {
        let p = [[10., 10.], [200., 10.], [200., 90.], [10., 90.]];
        let result = clip_polyline(&p, [0., 0., 100., 100.]);
        assert_eq!(result.len(), 2);
        assert_eq!(result[0][1], [100., 10.]);
        assert_eq!(result[1][0], [100., 90.]);
    }
    #[test]
    fn visible_closed_loop_is_unchanged() {
        let p = vec![[10., 10.], [30., 10.], [30., 30.], [10., 10.]];
        assert_eq!(clip_polyline(&p, [0., 0., 100., 100.]), vec![p]);
    }
    #[test]
    fn partially_visible_loop_retains_continuity() {
        let p = [[50., 50.], [200., 50.], [200., 80.], [50., 80.], [50., 50.]];
        let result = clip_polyline(&p, [0., 0., 100., 100.]);
        assert_eq!(result.len(), 1);
        assert_eq!(
            result[0],
            vec![[100., 80.], [50., 80.], [50., 50.], [100., 50.]]
        );
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Stroke {
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub widths: Vec<f32>,
    pub points: Vec<Point>,
    pub group: usize,
    pub collection: u32,
}
#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct Settings {
    pub threshold: f32,
    pub gap: f32,
    pub tolerance: f32,
    pub minimum: f32,
    pub wobble: f32,
    pub wavelength: f32,
    pub secondary: f32,
    pub secondary_wavelength: f32,
    pub size_jitter: f32,
    pub size_wavelength: f32,
    pub seed: u32,
}
impl Default for Settings {
    fn default() -> Self {
        Self {
            threshold: 0.5,
            gap: 0.,
            tolerance: 0.6,
            minimum: 0.,
            wobble: 0.,
            wavelength: 24.,
            secondary: 0.,
            secondary_wavelength: 12.,
            size_jitter: 0.,
            size_wavelength: 24.,
            seed: 1,
        }
    }
}
impl Settings {
    pub fn validate(&self) -> Result<()> {
        ensure!(
            (0.001..=0.999).contains(&self.threshold)
                && (0.0..=100.).contains(&self.gap)
                && (0.0..=20.).contains(&self.tolerance)
                && (0.0..=10000.).contains(&self.minimum)
                && (0.0..=20.).contains(&self.wobble)
                && (2.0..=500.).contains(&self.wavelength)
                && (0.0..=20.).contains(&self.secondary)
                && (2.0..=500.).contains(&self.secondary_wavelength)
                && (0.0..=100.).contains(&self.size_jitter)
                && (2.0..=500.).contains(&self.size_wavelength),
            "Invalid reconstruction settings"
        );
        Ok(())
    }
}
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Style {
    pub visible: bool,
    pub width: f32,
    pub opacity: f32,
}
impl Default for Style {
    fn default() -> Self {
        Self {
            visible: true,
            width: 6.,
            opacity: 1.,
        }
    }
}

#[derive(Clone, Serialize, Deserialize)]
pub struct Session {
    pub version: u32,
    pub fingerprint: String,
    pub settings: Settings,
    pub styles: [Style; 4],
    pub collection_styles: HashMap<u32, Style>,
    #[serde(default)]
    pub layers: HashMap<u32, Layer>,
    pub hidden: std::collections::HashSet<usize>,
    pub paths: Vec<Stroke>,
    #[serde(default)]
    pub raw: Vec<Stroke>,
    #[serde(default)]
    pub joins: Vec<[Point; 2]>,
}
impl Session {
    pub fn validate(&self, package: &Package) -> Result<()> {
        ensure!(
            (2..=4).contains(&self.version) && self.fingerprint == package.fingerprint,
            "Session belongs to a different package or version"
        );
        self.settings.validate()?;
        for layer in self.layers.values() {
            layer.validate()?;
        }
        for s in self.styles.iter().chain(self.collection_styles.values()) {
            ensure!(
                (0.01..=100.).contains(&s.width) && (0.0..=1.).contains(&s.opacity),
                "Invalid style"
            );
        }
        for p in self.paths.iter().chain(self.raw.iter()) {
            ensure!(
                (p.widths.is_empty()
                    || (p.widths.len() == p.points.len()
                        && p.widths.iter().all(|v| (0.01..=2.).contains(v))))
                    && p.group < 4
                    && p.points.len() >= 2
                    && p.points
                        .iter()
                        .flatten()
                        .all(|v| v.is_finite() && v.abs() < 1e7),
                "Invalid session paths"
            );
        }
        ensure!(
            self.joins
                .iter()
                .flatten()
                .flatten()
                .all(|v| v.is_finite() && v.abs() < 1e7),
            "Invalid session joins"
        );
        Ok(())
    }
}

#[cfg(test)]
pub fn fixture() -> Package {
    Package {
        manifest: Manifest {
            version: 1,
            width: 20,
            height: 10,
            files: HashMap::new(),
            geometry: vec![],
            collections: vec![
                Collection {
                    id: 1,
                    name: "Left".into(),
                    path: "Root / Left".into(),
                    color: [255, 0, 0],
                },
                Collection {
                    id: 2,
                    name: "Right".into(),
                    path: "Root / Right".into(),
                    color: [0, 255, 0],
                },
            ],
            mask_encoding: "srgb".into(),
        },
        folder: PathBuf::new(),
        fingerprint: "test".into(),
        background: image::RgbaImage::new(20, 10),
        id_image: Some(image::RgbaImage::new(20, 10)),
        ids: (0..200).map(|i| if i % 20 < 10 { 1 } else { 2 }).collect(),
        masks: std::array::from_fn(|_| vec![0.; 200]),
    }
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Layer {
    #[serde(default)]
    pub color: crate::colour::Grade,
    pub settings: Settings,
    pub styles: [Style; 4],
}
impl Layer {
    pub fn validate(&self) -> Result<()> {
        self.color.validate()?;
        self.settings.validate()?;
        for s in &self.styles {
            ensure!(
                (0.01..=100.).contains(&s.width) && (0.0..=1.).contains(&s.opacity),
                "Invalid layer style"
            );
        }
        Ok(())
    }
}
impl Settings {
    pub fn subtle() -> Self {
        Self {
            wobble: 1.,
            wavelength: 90.,
            secondary: 0.5,
            secondary_wavelength: 20.,
            size_jitter: 5.,
            size_wavelength: 5.,
            ..Default::default()
        }
    }
    pub fn copy_variation(&mut self, source: Self) {
        self.wobble = source.wobble;
        self.wavelength = source.wavelength;
        self.secondary = source.secondary;
        self.secondary_wavelength = source.secondary_wavelength;
        self.size_jitter = source.size_jitter;
        self.size_wavelength = source.size_wavelength;
        self.seed = source.seed;
    }
    pub fn reconstruction_key(mut self) -> Self {
        self.copy_variation(Self::default());
        self
    }
}
