use crate::model::{Layer, Package, Point};
use anyhow::{Result, ensure};
use image::RgbaImage;
use rayon::prelude::*;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;

#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize)]
pub struct Gradient {
    pub start: Point,
    pub end: Point,
    pub amount: f32,
}
impl Gradient {
    pub fn value(&self, p: Point, aspect: f32) -> f32 {
        let d = [
            (self.end[0] - self.start[0]) * aspect,
            self.end[1] - self.start[1],
        ];
        let length = d[0] * d[0] + d[1] * d[1];
        if length < 1e-10 {
            return 0.;
        }
        let t = (((p[0] - self.start[0]) * aspect * d[0] + (p[1] - self.start[1]) * d[1]) / length)
            .clamp(0., 1.);
        // Soft transitions at the ends of the ramp.
        t * t * (3. - 2. * t) * self.amount
    }
}
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct Grade {
    pub luminosity: f32,
    pub saturation: f32,
    pub hue: f32,
    pub contrast: f32,
    pub gradient: Option<Gradient>,
}
impl Default for Grade {
    fn default() -> Self {
        Self {
            luminosity: 0.,
            saturation: 1.,
            hue: 0.,
            contrast: 1.,
            gradient: None,
        }
    }
}
impl Grade {
    pub fn validate(&self) -> Result<()> {
        ensure!(
            (-100.0..=100.).contains(&self.luminosity)
                && (0.0..=2.).contains(&self.saturation)
                && (-180.0..=180.).contains(&self.hue)
                && (0.0..=2.).contains(&self.contrast),
            "Invalid colour adjustment"
        );
        if let Some(g) = self.gradient {
            ensure!(
                g.start
                    .iter()
                    .chain(g.end.iter())
                    .all(|v| (0.0..=1.).contains(v))
                    && (-100.0..=100.).contains(&g.amount),
                "Invalid colour gradient"
            );
        }
        Ok(())
    }
    fn pixel(&self, pixel: &mut [u8], position: Point, aspect: f32) {
        if pixel[3] == 0 || *self == Self::default() {
            return;
        }
        let rgb = [
            pixel[0] as f32 / 255.,
            pixel[1] as f32 / 255.,
            pixel[2] as f32 / 255.,
        ];
        let adjusted = self.adjust(rgb, position, aspect);
        for i in 0..3 {
            pixel[i] = (adjusted[i] * 255.).round() as u8;
        }
    }
    fn adjust(&self, rgb: [f32; 3], position: Point, aspect: f32) -> [f32; 3] {
        let max = rgb[0].max(rgb[1]).max(rgb[2]);
        let min = rgb[0].min(rgb[1]).min(rgb[2]);
        let delta = max - min;
        let mut light = (max + min) * 0.5;
        let saturation = if delta < 1e-6 {
            0.
        } else {
            delta / (1. - (2. * light - 1.).abs()).max(1e-6)
        };
        let hue = if delta < 1e-6 {
            0.
        } else if max == rgb[0] {
            ((rgb[1] - rgb[2]) / delta).rem_euclid(6.)
        } else if max == rgb[1] {
            (rgb[2] - rgb[0]) / delta + 2.
        } else {
            (rgb[0] - rgb[1]) / delta + 4.
        };
        let hue = (hue + self.hue / 60.).rem_euclid(6.);
        light = ((light - 0.5) * self.contrast
            + 0.5
            + (self.luminosity + self.gradient.map_or(0., |g| g.value(position, aspect))) / 100.)
            .clamp(0., 1.);
        let chroma = (1. - (2. * light - 1.).abs()) * (saturation * self.saturation).clamp(0., 1.);
        let x = chroma * (1. - (hue.rem_euclid(2.) - 1.).abs());
        let components = match hue as u32 {
            0 => [chroma, x, 0.],
            1 => [x, chroma, 0.],
            2 => [0., chroma, x],
            3 => [0., x, chroma],
            4 => [x, 0., chroma],
            _ => [chroma, 0., x],
        };
        components.map(|v| (v + light - chroma * 0.5).clamp(0., 1.))
    }
}
pub fn grades(layers: &HashMap<u32, Layer>) -> HashMap<u32, Grade> {
    layers
        .iter()
        .filter(|(_, l)| l.color != Grade::default())
        .map(|(&id, l)| (id, l.color.clone()))
        .collect()
}
pub struct Preview {
    pub image: RgbaImage,
    pub ids: Vec<u32>,
}
impl Preview {
    pub fn new(package: &Package) -> Self {
        let (w, h) = package.background.dimensions();
        let ratio = (1600. / w.max(h) as f32).min(1.);
        let (pw, ph) = (
            (w as f32 * ratio).round().max(1.) as u32,
            (h as f32 * ratio).round().max(1.) as u32,
        );
        let image = image::imageops::resize(
            &package.background,
            pw,
            ph,
            image::imageops::FilterType::Triangle,
        );
        let ids = (0..pw * ph)
            .map(|i| {
                let x = (((i % pw) as f32 + 0.5) * w as f32 / pw as f32) as u32;
                let y = (((i / pw) as f32 + 0.5) * h as f32 / ph as f32) as u32;
                package.ids[(y.min(h - 1) * w + x.min(w - 1)) as usize]
            })
            .collect();
        Self { image, ids }
    }
}
pub fn render(source: &RgbaImage, ids: &[u32], grades: &HashMap<u32, Grade>) -> RgbaImage {
    let mut result = source.clone();
    let (w, h) = source.dimensions();
    result.par_chunks_mut(4).enumerate().for_each(|(i, p)| {
        if let Some(grade) = grades.get(&ids[i]) {
            grade.pixel(
                p,
                [
                    ((i as u32 % w) as f32 + 0.5) / w as f32,
                    ((i as u32 / w) as f32 + 0.5) / h as f32,
                ],
                w as f32 / h as f32,
            );
        }
    });
    result
}
pub fn save(path: &std::path::Path, package: &Package, layers: &HashMap<u32, Layer>) -> Result<()> {
    let grades = grades(layers);
    // Read the source plate again for export: the interactive package uses RGBA8,
    // but Blender commonly writes 16-bit plates whose untouched values must survive.
    if let Some(relative) = package.manifest.files.get("flats") {
        let source = package.folder.join(relative).canonicalize()?;
        ensure!(
            source.starts_with(package.folder.canonicalize()?),
            "Colour source is outside package"
        );
        let original = image::open(source)?;
        let colour = original.color();
        if colour.bits_per_pixel() / colour.channel_count() as u16 > 8 {
            let mut image = original.to_rgba16();
            let (w, h) = image.dimensions();
            ensure!(
                (w, h) == package.background.dimensions(),
                "Colour plate dimensions changed; reopen the package"
            );
            image
                .as_mut()
                .par_chunks_mut(4)
                .enumerate()
                .for_each(|(i, p)| {
                    if p[3] == 0 {
                        return;
                    }
                    if let Some(grade) = grades.get(&package.ids[i]) {
                        let rgb = [
                            p[0] as f32 / 65535.,
                            p[1] as f32 / 65535.,
                            p[2] as f32 / 65535.,
                        ];
                        let rgb = grade.adjust(
                            rgb,
                            [
                                ((i as u32 % w) as f32 + 0.5) / w as f32,
                                ((i as u32 / w) as f32 + 0.5) / h as f32,
                            ],
                            w as f32 / h as f32,
                        );
                        for c in 0..3 {
                            p[c] = (rgb[c] * 65535.).round() as u16;
                        }
                    }
                });
            image.save(path)?;
            return Ok(());
        }
    }
    render(&package.background, &package.ids, &grades).save(path)?;
    Ok(())
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn sixteen_bit_export_preserves_exact_unedited_samples() {
        let mut package = crate::model::fixture();
        let folder = std::env::temp_dir().join(format!("lazy-colour16-{}", std::process::id()));
        std::fs::create_dir_all(&folder).unwrap();
        let original =
            image::ImageBuffer::from_pixel(20, 10, image::Rgba([12345u16, 23456, 34567, 45678]));
        original.save(folder.join("flats.png")).unwrap();
        package.folder = folder.canonicalize().unwrap();
        package
            .manifest
            .files
            .insert("flats".into(), "flats.png".into());
        package.background = image::DynamicImage::ImageRgba16(original.clone()).to_rgba8();
        let layer = Layer {
            color: Grade {
                luminosity: 10.,
                ..Default::default()
            },
            settings: Default::default(),
            styles: std::array::from_fn(|_| Default::default()),
        };
        save(
            &folder.join("output.png"),
            &package,
            &HashMap::from([(1, layer)]),
        )
        .unwrap();
        let saved = image::open(folder.join("output.png")).unwrap();
        assert_eq!(saved.color(), image::ColorType::Rgba16);
        let saved = saved.to_rgba16();
        assert_eq!(saved.get_pixel(15, 5), original.get_pixel(15, 5));
        assert!(saved.pixels().all(|p| p[3] == 45678));
        assert!(saved.get_pixel(5, 5)[0] > 12345);
        std::fs::remove_file(folder.join("output.png")).unwrap();
        std::fs::remove_file(folder.join("flats.png")).unwrap();
    }
    #[test]
    fn full_resolution_png_and_old_layers() {
        let mut package = crate::model::fixture();
        for p in package.background.pixels_mut() {
            *p = image::Rgba([80, 100, 120, 123]);
        }
        let layer = Layer {
            color: Grade {
                luminosity: 20.,
                ..Default::default()
            },
            settings: Default::default(),
            styles: std::array::from_fn(|_| Default::default()),
        };
        let path =
            std::env::temp_dir().join(format!("lazy-colour-test-{}.png", std::process::id()));
        save(&path, &package, &HashMap::from([(1, layer.clone())])).unwrap();
        let saved = image::open(&path).unwrap().to_rgba8();
        assert_eq!(saved.dimensions(), (20, 10));
        assert_eq!(saved.get_pixel(15, 5), package.background.get_pixel(15, 5));
        assert!(saved.get_pixel(5, 5)[0] > 80);
        assert!(saved.pixels().all(|p| p[3] == 123));
        std::fs::remove_file(path).unwrap();
        let mut old = serde_json::to_value(&layer).unwrap();
        old.as_object_mut().unwrap().remove("color");
        let old: Layer = serde_json::from_value(old).unwrap();
        assert_eq!(old.color, Grade::default());
        let gradient = Gradient {
            start: [0., 0.],
            end: [1., 1.],
            amount: 100.,
        };
        // A 2:1 image: this pixel lies on the physical perpendicular at the midpoint.
        assert!((gradient.value([0.625, 0.], 2.) - 50.).abs() < 0.001);
    }
    #[test]
    fn colour_is_local_and_preserves_alpha() {
        let source = RgbaImage::from_raw(2, 1, vec![255, 0, 0, 127, 255, 0, 0, 255]).unwrap();
        let result = render(
            &source,
            &[1, 2],
            &HashMap::from([(
                1,
                Grade {
                    hue: 120.,
                    ..Default::default()
                },
            )]),
        );
        assert_eq!(result.get_pixel(0, 0).0, [0, 255, 0, 127]);
        assert_eq!(result.get_pixel(1, 0), source.get_pixel(1, 0));
        assert_eq!(render(&source, &[1, 2], &HashMap::new()), source);
    }
    #[test]
    fn gradient_replaces_and_roundtrips() {
        let mut grade = Grade {
            gradient: Some(Gradient {
                start: [0., 0.],
                end: [1., 0.],
                amount: 30.,
            }),
            ..Default::default()
        };
        assert_eq!(grade.gradient.unwrap().value([0., 0.], 1.), 0.);
        assert_eq!(grade.gradient.unwrap().value([1., 0.], 1.), 30.);
        assert_eq!(grade.gradient.unwrap().value([0.5, 0.], 1.), 15.);
        grade.gradient = Some(Gradient {
            start: [0., 1.],
            end: [1., 1.],
            amount: -20.,
        });
        let saved: Grade = serde_json::from_slice(&serde_json::to_vec(&grade).unwrap()).unwrap();
        assert_eq!(saved, grade);
        saved.validate().unwrap();
        assert_eq!(saved.gradient.unwrap().value([1., 1.], 1.), -20.);
        assert_eq!(
            serde_json::from_str::<Grade>("{}").unwrap(),
            Grade::default()
        );
    }
    #[test]
    fn luminosity_saturation_and_invalid_values() {
        let mut p = [180, 80, 40, 255];
        Grade {
            saturation: 0.,
            ..Default::default()
        }
        .pixel(&mut p, [0., 0.], 1.);
        assert_eq!(p[0], p[1]);
        assert_eq!(p[1], p[2]);
        Grade {
            luminosity: 100.,
            ..Default::default()
        }
        .pixel(&mut p, [0., 0.], 1.);
        assert_eq!(p, [255, 255, 255, 255]);
        assert!(
            Grade {
                hue: f32::NAN,
                ..Default::default()
            }
            .validate()
            .is_err()
        );
    }
}
