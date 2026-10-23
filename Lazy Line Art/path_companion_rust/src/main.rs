mod colour;
mod export;
mod model;
mod paths;
mod preferences;
mod ribbon;
mod ui;

use std::{path::PathBuf, sync::Arc};

fn main() -> anyhow::Result<()> {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args
        .first()
        .is_some_and(|s| s == "--benchmark" || s == "--check")
    {
        let folder = PathBuf::from(
            args.get(1)
                .ok_or_else(|| anyhow::anyhow!("Supply a package folder"))?,
        );
        let package = model::Package::load(&folder)?;
        let mut processor = paths::Processor::default();
        let settings = model::Settings::default();
        let result = processor.process(&package, settings);
        println!(
            "{}x{}: {} input paths, {} output paths, {} points; initial {:0.2} ms",
            package.manifest.width,
            package.manifest.height,
            result.raw.len(),
            result.paths.len(),
            result.paths.iter().map(|p| p.points.len()).sum::<usize>(),
            result.milliseconds
        );
        for amp in [0.5, 1., 2., 3.] {
            let result = processor.process(
                &package,
                model::Settings {
                    wobble: amp,
                    ..settings
                },
            );
            println!(
                "cached wobble amplitude {amp}: {:0.2} ms",
                result.milliseconds
            );
        }
        let session = model::Session {
            version: 2,
            fingerprint: package.fingerprint.clone(),
            settings,
            styles: std::array::from_fn(|_| model::Style::default()),
            collection_styles: Default::default(),
            layers: Default::default(),
            hidden: Default::default(),
            paths: result.paths,
            raw: result.raw,
            joins: result.joins,
        };
        if args[0] == "--benchmark" {
            export::save_svg(&folder.join("rust_benchmark.svg"), &package, &session)?;
        }
        return Ok(());
    }
    let variation = args.first().is_some_and(|s| s == "--smoke-variation");
    let colour_smoke = args.first().is_some_and(|s| s == "--smoke-colour");
    let smoke = colour_smoke || variation || args.first().is_some_and(|s| s == "--smoke");
    let folder = args.get(if smoke { 1 } else { 0 }).map(PathBuf::from);
    let package = folder
        .map(|p| model::Package::load(&p).map(Arc::new))
        .transpose()?;
    let options = eframe::NativeOptions {
        viewport: eframe::egui::ViewportBuilder::default().with_inner_size([1400., 900.]),
        multisampling: 4,
        ..Default::default()
    };
    eframe::run_native(
        "Lazy Path Companion — Rust",
        options,
        Box::new(move |cc| {
            Ok(Box::new(ui::App::new(
                cc,
                package,
                smoke,
                variation,
                colour_smoke,
            )))
        }),
    )
    .map_err(|e| anyhow::anyhow!("{e}"))
}
