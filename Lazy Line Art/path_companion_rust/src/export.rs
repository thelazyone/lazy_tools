use crate::model::*;
use anyhow::Result;
use std::{
    collections::{HashMap, HashSet},
    fmt::Write,
    path::Path,
};

pub fn effective_style(
    group: usize,
    collection: u32,
    styles: &[Style; 4],
    collections: &HashMap<u32, Style>,
    layers: &HashMap<u32, Layer>,
) -> Style {
    let g = &layers.get(&collection).map_or(styles, |l| &l.styles)[group];
    if let Some(c) = collections.get(&collection) {
        Style {
            visible: g.visible && c.visible,
            width: g.width * c.width,
            opacity: g.opacity * c.opacity,
        }
    } else {
        g.clone()
    }
}
fn escape(s: &str) -> String {
    s.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
        .replace('\'', "&apos;")
}
pub fn svg(
    package: &Package,
    paths: &[Stroke],
    styles: &[Style; 4],
    collections: &HashMap<u32, Style>,
    layers: &HashMap<u32, Layer>,
    hidden: &HashSet<usize>,
) -> String {
    let mut text = format!(
        "<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"{}\" height=\"{}\" viewBox=\"0 0 {} {}\">\n",
        package.manifest.width,
        package.manifest.height,
        package.manifest.width,
        package.manifest.height
    );
    let mut ids: Vec<u32> = paths.iter().map(|p| p.collection).collect();
    ids.sort();
    ids.dedup();
    for id in ids {
        let name = package
            .manifest
            .collections
            .iter()
            .find(|c| c.id == id)
            .map(|c| c.path.as_str())
            .unwrap_or("Unassigned / legacy package");
        writeln!(
            text,
            "<g id=\"collection-{id}\"><title>{}</title>",
            escape(name)
        )
        .unwrap();
        for (group, name) in GROUPS.iter().enumerate() {
            let style = effective_style(group, id, styles, collections, layers);
            if !style.visible {
                continue;
            }
            writeln!(text,"<g id=\"collection-{id}-group-{group}\" fill=\"none\" stroke=\"#171717\" stroke-width=\"{}\" stroke-opacity=\"{}\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><title>{name}</title>",style.width,style.opacity).unwrap();
            for (i, p) in paths
                .iter()
                .enumerate()
                .filter(|(i, p)| p.collection == id && p.group == group && !hidden.contains(i))
            {
                if !p.widths.is_empty() {
                    let outline = crate::ribbon::outline(&p.points, &p.widths, style.width);
                    write!(text, "<path id=\"stroke-{i}\" fill=\"#171717\" fill-opacity=\"{}\" stroke=\"none\" d=\"M", style.opacity).unwrap();
                    for (j, point) in outline.iter().enumerate() {
                        if j > 0 {
                            text.push_str(" L");
                        }
                        write!(text, "{:0.4},{:0.4}", point[0], point[1]).unwrap();
                    }
                    text.push_str(" Z\"/>\n");
                    continue;
                }
                write!(text, "<path id=\"stroke-{i}\" d=\"M").unwrap();
                for (j, point) in p.points.iter().enumerate() {
                    if j > 0 {
                        text.push_str(" L");
                    }
                    write!(text, "{:0.4},{:0.4}", point[0], point[1]).unwrap();
                }
                if p.points.first() == p.points.last() {
                    text.push_str(" Z");
                }
                text.push_str("\"/>\n");
            }
            text.push_str("</g>\n");
        }
        text.push_str("</g>\n");
    }
    text.push_str("</svg>\n");
    text
}
pub fn save_svg(file: &Path, package: &Package, session: &Session) -> Result<()> {
    std::fs::write(
        file,
        svg(
            package,
            &session.paths,
            &session.styles,
            &session.collection_styles,
            &session.layers,
            &session.hidden,
        ),
    )?;
    std::fs::write(
        file.with_extension("paths.json"),
        serde_json::to_vec(session)?,
    )?;
    Ok(())
}
pub fn save_ids(folder: &Path, package: &Package) -> Result<()> {
    anyhow::ensure!(
        package.id_image.is_some(),
        "This package has no collection IDs. Export again using Lazy Line Art 0.2.1."
    );
    package
        .id_image
        .as_ref()
        .unwrap()
        .save(folder.join("collection_ids.png"))?;
    std::fs::write(
        folder.join("collection_palette.json"),
        serde_json::to_vec_pretty(&package.manifest.collections)?,
    )?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn layer_group_style_and_visibility_export() {
        let mut styles = std::array::from_fn(|_| Style::default());
        styles[0].width = 9.;
        styles[0].opacity = 0.4;
        let layer = Layer {
            color: Default::default(),
            settings: Settings::subtle(),
            styles,
        };
        let layers = HashMap::from([(1, layer.clone())]);
        let groups = std::array::from_fn(|_| Style::default());
        let stroke = Stroke {
            points: vec![[1., 1.], [3., 2.]],
            widths: vec![],
            group: 0,
            collection: 1,
        };
        let text = svg(
            &crate::model::fixture(),
            std::slice::from_ref(&stroke),
            &groups,
            &HashMap::new(),
            &layers,
            &HashSet::new(),
        );
        assert!(text.contains("stroke-width=\"9\" stroke-opacity=\"0.4\""));
        let hidden = HashMap::from([(
            1,
            Style {
                visible: false,
                width: 1.,
                opacity: 1.,
            },
        )]);
        let text = svg(
            &crate::model::fixture(),
            &[stroke],
            &groups,
            &hidden,
            &layers,
            &HashSet::new(),
        );
        assert!(!text.contains("id=\"stroke-0\""));
        let restored: Layer = serde_json::from_slice(&serde_json::to_vec(&layer).unwrap()).unwrap();
        assert_eq!(restored, layer);
        restored.validate().unwrap();
    }
    #[test]
    fn variable_width_exports_and_legacy_defaults() {
        let settings: Settings = serde_json::from_str(r#"{"threshold":0.5,"gap":0,"tolerance":0.6,"minimum":0,"wobble":2,"wavelength":24,"seed":1}"#).unwrap();
        assert_eq!(settings.secondary, 0.);
        assert_eq!(settings.size_jitter, 0.);
        settings.validate().unwrap();
        let old: Stroke =
            serde_json::from_str(r#"{"points":[[1,1],[3,2]],"group":0,"collection":1}"#).unwrap();
        assert!(old.widths.is_empty());
        let stroke = Stroke {
            widths: vec![0.5, 1.5],
            ..old
        };
        let restored: Stroke =
            serde_json::from_slice(&serde_json::to_vec(&stroke).unwrap()).unwrap();
        assert_eq!(restored.widths, stroke.widths);
        let text = svg(
            &crate::model::fixture(),
            &[stroke],
            &std::array::from_fn(|_| Style::default()),
            &HashMap::new(),
            &HashMap::new(),
            &HashSet::new(),
        );
        assert!(text.contains("fill-opacity=\"1\" stroke=\"none\""));
        assert!(text.contains("id=\"stroke-0\""));
    }
    #[test]
    fn svg_collection_groups_and_session_roundtrip() {
        let package = crate::model::fixture();
        let paths = vec![
            Stroke {
                widths: Vec::new(),
                points: vec![[1., 1.], [3., 2.]],
                group: 0,
                collection: 1,
            },
            Stroke {
                widths: Vec::new(),
                points: vec![[12., 1.], [15., 2.]],
                group: 3,
                collection: 2,
            },
        ];
        let session = Session {
            version: 2,
            fingerprint: package.fingerprint.clone(),
            settings: Default::default(),
            styles: std::array::from_fn(|_| Style::default()),
            collection_styles: HashMap::new(),
            layers: HashMap::new(),
            hidden: HashSet::from([0]),
            paths,
            raw: vec![],
            joins: vec![],
        };
        let roundtrip: Session =
            serde_json::from_slice(&serde_json::to_vec(&session).unwrap()).unwrap();
        roundtrip.validate(&package).unwrap();
        let svg = svg(
            &package,
            &roundtrip.paths,
            &roundtrip.styles,
            &roundtrip.collection_styles,
            &roundtrip.layers,
            &roundtrip.hidden,
        );
        assert!(svg.contains("collection-1"));
        assert!(svg.contains("collection-2"));
        assert!(!svg.contains("id=\"stroke-0\""));
        assert!(svg.contains("id=\"stroke-1\""));
        let mut wrong = roundtrip;
        wrong.fingerprint = "other".into();
        assert!(wrong.validate(&package).is_err());
    }
}
