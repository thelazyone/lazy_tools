use crate::model::*;
use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};

#[derive(Clone, Serialize, Deserialize)]
pub struct Preferences {
    pub variation: Settings,
    pub styles: [Style; 4],
    pub presets: Vec<(String, Settings)>,
}
impl Default for Preferences {
    fn default() -> Self {
        Self {
            variation: Settings::subtle(),
            styles: std::array::from_fn(|_| Style::default()),
            presets: vec![],
        }
    }
}
impl Preferences {
    pub fn validate(&self) -> Result<()> {
        Layer {
            color: Default::default(),
            settings: self.variation,
            styles: self.styles.clone(),
        }
        .validate()?;
        for (_, preset) in &self.presets {
            preset.validate()?;
        }
        Ok(())
    }
    pub fn load(path: &Path) -> Result<Self> {
        if !path.exists() {
            return Ok(Self::default());
        }
        let prefs: Self = serde_json::from_slice(&std::fs::read(path)?)?;
        prefs.validate()?;
        Ok(prefs)
    }
    pub fn save(&self, path: &Path) -> Result<()> {
        self.validate()?;
        std::fs::create_dir_all(path.parent().context("Missing settings folder")?)?;
        // Keep a recoverable previous copy if a write is interrupted.
        if path.exists() {
            std::fs::copy(path, path.with_extension("json.bak"))?;
        }
        std::fs::write(path, serde_json::to_vec_pretty(self)?)?;
        Ok(())
    }
}
pub fn file() -> PathBuf {
    let exe = std::env::current_exe().unwrap_or_default();
    for parent in exe.ancestors().skip(1) {
        if parent.join("path_companion_rust").is_dir() {
            return parent.join("settings/defaults.json");
        }
    }
    exe.parent()
        .unwrap_or(Path::new("."))
        .join("settings/defaults.json")
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn preferences_persist_styles_variation_and_presets() {
        let folder =
            std::env::temp_dir().join(format!("lazy-path-preferences-{}", std::process::id()));
        let path = folder.join("defaults.json");
        let mut prefs = Preferences::default();
        assert_eq!(prefs.styles[0].width, 6.);
        assert_eq!(prefs.variation.wavelength, 90.);
        prefs.styles[2].width = 4.;
        prefs.presets.push(("Test".into(), Settings::subtle()));
        prefs.save(&path).unwrap();
        let loaded = Preferences::load(&path).unwrap();
        assert_eq!(loaded.styles, prefs.styles);
        assert_eq!(loaded.variation, prefs.variation);
        assert_eq!(loaded.presets, prefs.presets);
        std::fs::remove_file(path).unwrap();
    }
}
