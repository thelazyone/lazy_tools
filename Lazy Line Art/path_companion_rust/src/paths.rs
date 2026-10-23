use crate::model::*;
use rayon::prelude::*;
use std::collections::{BTreeMap, HashMap, HashSet};

pub fn distance(a: Point, b: Point) -> f32 {
    ((a[0] - b[0]).powi(2) + (a[1] - b[1]).powi(2)).sqrt()
}
fn lerp(a: Point, b: Point, t: f32) -> Point {
    [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t]
}
fn direction(a: Point, b: Point) -> Point {
    let d = distance(a, b).max(1e-12);
    [(b[0] - a[0]) / d, (b[1] - a[1]) / d]
}
fn dot(a: Point, b: Point) -> f32 {
    a[0] * b[0] + a[1] * b[1]
}
pub fn length(p: &[Point]) -> f32 {
    p.windows(2).map(|p| distance(p[0], p[1])).sum()
}

/// Marching squares: interpolated contours, with a deterministic saddle rule.
/// Grid-edge keys share vertices exactly, so tracing doesn't use proximity.
pub fn contours(pixels: &[f32], w: usize, h: usize, threshold: f32) -> Vec<Vec<Point>> {
    let mut vertices = Vec::<Point>::new();
    let mut lookup = HashMap::<usize, usize>::new();
    let mut edges = Vec::<[usize; 2]>::new();
    for y in 0..h - 1 {
        for x in 0..w - 1 {
            let v = [
                pixels[y * w + x],
                pixels[y * w + x + 1],
                pixels[(y + 1) * w + x + 1],
                pixels[(y + 1) * w + x],
            ];
            let high = v.map(|v| v > threshold);
            let corners = [
                [x as f32 + 0.5, y as f32 + 0.5],
                [x as f32 + 1.5, y as f32 + 0.5],
                [x as f32 + 1.5, y as f32 + 1.5],
                [x as f32 + 0.5, y as f32 + 1.5],
            ];
            let keys = [
                2 * (y * w + x),
                2 * (y * w + x + 1) + 1,
                2 * ((y + 1) * w + x),
                2 * (y * w + x) + 1,
            ];
            let mut cross = Vec::with_capacity(4);
            for e in 0..4 {
                let n = (e + 1) % 4;
                if high[e] != high[n] {
                    let index = *lookup.entry(keys[e]).or_insert_with(|| {
                        let p = lerp(
                            corners[e],
                            corners[n],
                            ((threshold - v[e]) / (v[n] - v[e])).clamp(0., 1.),
                        );
                        vertices.push(p);
                        vertices.len() - 1
                    });
                    cross.push((e, index));
                }
            }
            if cross.len() == 2 {
                edges.push([cross[0].1, cross[1].1]);
            } else if cross.len() == 4 {
                // Asymptotic decider for the bilinear cell, with fixed tie-breaking.
                let q = (v[0] - threshold) * (v[2] - threshold)
                    - (v[1] - threshold) * (v[3] - threshold);
                if q >= 0. {
                    edges.push([cross[0].1, cross[1].1]);
                    edges.push([cross[2].1, cross[3].1]);
                } else {
                    edges.push([cross[0].1, cross[3].1]);
                    edges.push([cross[1].1, cross[2].1]);
                }
            }
        }
    }
    let mut adjacency = vec![Vec::new(); vertices.len()];
    for (i, e) in edges.iter().enumerate() {
        adjacency[e[0]].push(i);
        adjacency[e[1]].push(i);
    }
    let mut used = vec![false; edges.len()];
    let starts: Vec<usize> = (0..vertices.len())
        .filter(|&v| adjacency[v].len() == 1)
        .chain(0..vertices.len())
        .collect();
    let mut paths = Vec::new();
    for start in starts {
        if adjacency[start].iter().all(|&e| used[e]) {
            continue;
        }
        let mut current = start;
        let mut path = vec![vertices[start]];
        while let Some(&e) = adjacency[current].iter().find(|&&e| !used[e]) {
            used[e] = true;
            current = if edges[e][0] == current {
                edges[e][1]
            } else {
                edges[e][0]
            };
            path.push(vertices[current]);
            if current == start {
                break;
            }
        }
        if path.len() >= 2 {
            paths.push(path);
        }
    }
    paths
}

pub fn assign_collections(p: &[Point], group: usize, package: &Package) -> Vec<Stroke> {
    if p.len() < 2 {
        return vec![];
    }
    if package.id_image.is_none() {
        return vec![Stroke {
            widths: Vec::new(),
            points: p.to_vec(),
            group,
            collection: 0,
        }];
    }
    let mut sampled = vec![p[0]];
    for pair in p.windows(2) {
        let n = distance(pair[0], pair[1]).ceil().max(1.) as usize;
        // Camera-projected geometry should be near the canvas; cap pathological segments.
        let n = n.min(100_000);
        for i in 1..=n {
            sampled.push(lerp(pair[0], pair[1], i as f32 / n as f32));
        }
    }
    let mut id = package.collection_at(sampled[0]);
    let mut points = vec![sampled[0]];
    let mut result = Vec::new();
    for pair in sampled.windows(2) {
        let next = package.collection_at(pair[1]);
        if next != id {
            let boundary = lerp(pair[0], pair[1], 0.5);
            points.push(boundary);
            result.push(Stroke {
                widths: Vec::new(),
                points,
                group,
                collection: id,
            });
            points = vec![boundary];
            id = next;
        }
        points.push(pair[1]);
    }
    result.push(Stroke {
        widths: Vec::new(),
        points,
        group,
        collection: id,
    });
    if p[0] == *p.last().unwrap()
        && result.len() > 1
        && result[0].collection == result.last().unwrap().collection
    {
        let mut last = result.pop().unwrap().points;
        last.extend_from_slice(&result[0].points[1..]);
        result[0].points = last;
    }
    result
}

pub fn simplify(p: &[Point], tolerance: f32) -> Vec<Point> {
    if p.len() < 3 || tolerance <= 0. {
        return p.to_vec();
    }
    if p[0] == *p.last().unwrap() {
        let split = (1..p.len() - 1)
            .max_by(|&a, &b| distance(p[0], p[a]).total_cmp(&distance(p[0], p[b])))
            .unwrap_or(1);
        let mut first = simplify(&p[..=split], tolerance);
        first.pop();
        first.extend(simplify(&p[split..], tolerance));
        return first;
    }
    let mut keep = vec![false; p.len()];
    keep[0] = true;
    keep[p.len() - 1] = true;
    let mut stack = vec![(0, p.len() - 1)];
    while let Some((a, b)) = stack.pop() {
        if b <= a + 1 {
            continue;
        }
        let mut far = (0., a);
        for i in a + 1..b {
            let d = segment_distance(p[i], p[a], p[b]);
            if d > far.0 {
                far = (d, i);
            }
        }
        if far.0 > tolerance {
            keep[far.1] = true;
            stack.push((a, far.1));
            stack.push((far.1, b));
        }
    }
    p.iter()
        .zip(keep)
        .filter_map(|(&p, k)| k.then_some(p))
        .collect()
}
pub fn segment_distance(p: Point, a: Point, b: Point) -> f32 {
    let v = [b[0] - a[0], b[1] - a[1]];
    let t = (dot([p[0] - a[0], p[1] - a[1]], v) / dot(v, v).max(1e-20)).clamp(0., 1.);
    distance(p, lerp(a, b, t))
}

/// Uniform-grid neighbour lookup. Endpoints are matched only inside one collection/group.
pub fn join(lines: Vec<Vec<Point>>, gap: f32) -> (Vec<Vec<Point>>, Vec<[Point; 2]>) {
    if gap <= 0. {
        return (lines, vec![]);
    }
    let mut endpoints = Vec::<(usize, usize, Point, Point)>::new();
    let mut cells = HashMap::<(i32, i32), Vec<usize>>::new();
    for (i, line) in lines.iter().enumerate() {
        if line.len() < 2 || distance(line[0], *line.last().unwrap()) < 1e-6 {
            continue;
        }
        for side in 0..2 {
            let endpoint = if side == 0 {
                line[0]
            } else {
                *line.last().unwrap()
            };
            let inner = if side == 0 {
                line.iter().copied().find(|&p| distance(endpoint, p) > 0.01)
            } else {
                line.iter()
                    .rev()
                    .copied()
                    .find(|&p| distance(endpoint, p) > 0.01)
            };
            if let Some(inner) = inner {
                let cell = (
                    (endpoint[0] / gap).floor() as i32,
                    (endpoint[1] / gap).floor() as i32,
                );
                cells.entry(cell).or_default().push(endpoints.len());
                endpoints.push((i, side, endpoint, direction(inner, endpoint)));
            }
        }
    }
    let cosine = 25f32.to_radians().cos();
    let mut candidates = Vec::new();
    for (a, &(i, _, p, t)) in endpoints.iter().enumerate() {
        let cell = ((p[0] / gap).floor() as i32, (p[1] / gap).floor() as i32);
        for dy in -1..=1 {
            for dx in -1..=1 {
                if let Some(bucket) = cells.get(&(cell.0 + dx, cell.1 + dy)) {
                    for &b in bucket {
                        if b <= a {
                            continue;
                        }
                        let (j, _, q, u) = endpoints[b];
                        let d = distance(p, q);
                        if i == j || d > gap || dot(t, [-u[0], -u[1]]) < cosine {
                            continue;
                        }
                        if d > 0.01 {
                            let v = direction(p, q);
                            if dot(t, v) < cosine || dot(u, [-v[0], -v[1]]) < cosine {
                                continue;
                            }
                        }
                        candidates.push((d, a, b));
                    }
                }
            }
        }
    }
    candidates.sort_by(|a, b| a.0.total_cmp(&b.0).then(a.1.cmp(&b.1)).then(a.2.cmp(&b.2)));
    let mut links = HashMap::<(usize, usize), (usize, usize)>::new();
    let mut bridges = Vec::new();
    for (_, a, b) in candidates {
        let ea = (endpoints[a].0, endpoints[a].1);
        let eb = (endpoints[b].0, endpoints[b].1);
        if links.contains_key(&ea) || links.contains_key(&eb) {
            continue;
        }
        links.insert(ea, eb);
        links.insert(eb, ea);
        bridges.push([endpoints[a].2, endpoints[b].2]);
    }
    let mut starts = Vec::new();
    for i in 0..lines.len() {
        for side in 0..2 {
            if !links.contains_key(&(i, side)) {
                starts.push((i, side));
            }
        }
    }
    starts.extend((0..lines.len()).map(|i| (i, 0)));
    let mut seen = HashSet::new();
    let mut result = Vec::new();
    for (mut i, mut side) in starts {
        if seen.contains(&i) {
            continue;
        }
        let mut line = Vec::new();
        while seen.insert(i) {
            if side == 0 {
                line.extend_from_slice(&lines[i]);
            } else {
                line.extend(lines[i].iter().rev().copied());
            }
            if let Some(&(next, next_side)) = links.get(&(i, 1 - side)) {
                i = next;
                side = next_side;
                if seen.contains(&i) && line[0] != *line.last().unwrap() {
                    line.push(line[0]);
                }
            } else {
                break;
            }
        }
        result.push(line);
    }
    (result, bridges)
}

fn random(seed: u32, index: u32) -> f32 {
    let mut x = seed.wrapping_add(index.wrapping_mul(0x9e3779b9));
    x ^= x >> 16;
    x = x.wrapping_mul(0x7feb352d);
    x ^= x >> 15;
    x = x.wrapping_mul(0x846ca68b);
    x ^= x >> 16;
    (x as f64 / u32::MAX as f64 * 2. - 1.) as f32
}
/// Smooth, seeded normal displacement in output pixels, sampled by arc length.
/// Endpoints stay pinned; closed loops use periodic noise and retain exact closure.
fn vary(p: &[Point], settings: Settings, seed: u32) -> (Vec<Point>, Vec<f32>) {
    let amplitude = settings.wobble;
    let wavelength = settings.wavelength;
    if (amplitude <= 0. && settings.secondary <= 0. && settings.size_jitter <= 0.) || p.len() < 2 {
        return (p.to_vec(), Vec::new());
    }
    let mut arc = vec![0.];
    for s in p.windows(2) {
        arc.push(arc.last().unwrap() + distance(s[0], s[1]));
    }
    let total = *arc.last().unwrap();
    if total < 1e-6 {
        return (p.to_vec(), Vec::new());
    }
    let closed = p[0] == *p.last().unwrap();
    let mut shortest = 500.0_f32;
    for (strength, wave) in [
        (amplitude, wavelength),
        (settings.secondary, settings.secondary_wavelength),
        (settings.size_jitter, settings.size_wavelength),
    ] {
        if strength > 0. {
            shortest = shortest.min(wave);
        }
    }
    let noise = |s: f32, wavelength: f32, seed: u32, pin: bool| {
        let periods = (total / wavelength).round().max(1.) as u32;
        let wave = if closed {
            total / periods as f32
        } else {
            wavelength
        };
        let pos = s / wave;
        let index = pos.floor() as u32;
        let f = pos.fract();
        let smooth = f * f * (3. - 2. * f);
        let key = |i| if closed { i % periods } else { i };
        let value =
            random(seed, key(index)) * (1. - smooth) + random(seed, key(index + 1)) * smooth;
        let fade = if closed || !pin {
            1.
        } else {
            (s / (wave * 0.25))
                .min((total - s) / (wave * 0.25))
                .clamp(0., 1.)
        };
        value * fade
    };
    let count = (total / (shortest / 8.).clamp(0.25, 3.)).ceil().max(2.) as usize;
    let at = |s: f32| {
        let i = arc
            .partition_point(|&v| v < s)
            .saturating_sub(1)
            .min(p.len() - 2);
        lerp(
            p[i],
            p[i + 1],
            ((s - arc[i]) / (arc[i + 1] - arc[i]).max(1e-12)).clamp(0., 1.),
        )
    };
    let mut samples: Vec<f32> = (0..=count)
        .map(|i| total * i as f32 / count as f32)
        .chain(arc.iter().copied())
        .collect();
    samples.sort_by(f32::total_cmp);
    samples.dedup_by(|a, b| (*a - *b).abs() < 1e-5);
    let mut result = Vec::with_capacity(samples.len());
    let mut widths = Vec::new();
    for s in samples {
        let p = at(s);
        let epsilon = (shortest / 16.).clamp(0.1, 2.);
        let before = if closed {
            (s - epsilon).rem_euclid(total)
        } else {
            (s - epsilon).max(0.)
        };
        let after = if closed {
            (s + epsilon).rem_euclid(total)
        } else {
            (s + epsilon).min(total)
        };
        let tangent = direction(at(before), at(after));
        let offset = amplitude * noise(s, wavelength, seed, true)
            + settings.secondary
                * noise(
                    s,
                    settings.secondary_wavelength,
                    seed.wrapping_add(0x91ab),
                    true,
                );
        if settings.size_jitter > 0. {
            widths.push(
                (1. + settings.size_jitter / 100.
                    * noise(
                        s,
                        settings.size_wavelength,
                        seed.wrapping_add(0x73de),
                        false,
                    ))
                .max(0.01),
            );
        }
        result.push([p[0] - tangent[1] * offset, p[1] + tangent[0] * offset]);
    }
    if closed {
        if !widths.is_empty() {
            let first = widths[0];
            *widths.last_mut().unwrap() = first;
        }
        let first = result[0];
        *result.last_mut().unwrap() = first;
    } else {
        result[0] = p[0];
        *result.last_mut().unwrap() = *p.last().unwrap();
    }
    (result, widths)
}

#[cfg(test)]
pub fn wobble(p: &[Point], amplitude: f32, wavelength: f32, seed: u32) -> Vec<Point> {
    vary(
        p,
        Settings {
            wobble: amplitude,
            wavelength,
            ..Default::default()
        },
        seed,
    )
    .0
}

#[derive(Clone)]
pub struct Processed {
    pub raw: Vec<Stroke>,
    pub paths: Vec<Stroke>,
    pub joins: Vec<[Point; 2]>,
    pub milliseconds: f64,
}
#[derive(Default)]
pub struct Processor {
    thresholds: Vec<(u32, u32)>,
    raw_cache: BTreeMap<u32, Vec<Stroke>>,
    layer_keys: BTreeMap<u32, Settings>,
    raw: Vec<Stroke>,
    base_settings: Option<Settings>,
    base: Vec<Stroke>,
    joins: Vec<[Point; 2]>,
}
impl Processor {
    pub fn process(&mut self, package: &Package, settings: Settings) -> Processed {
        self.process_layers(package, settings, &std::collections::HashMap::new())
    }
    pub fn process_layers(
        &mut self,
        package: &Package,
        settings: Settings,
        layers: &std::collections::HashMap<u32, Layer>,
    ) -> Processed {
        let started = std::time::Instant::now();
        let mut thresholds: Vec<_> = layers
            .iter()
            .map(|(&id, l)| (id, l.settings.threshold.to_bits()))
            .collect();
        thresholds.sort_unstable();
        thresholds.push((u32::MAX, settings.threshold.to_bits()));
        if self.thresholds != thresholds || self.raw_cache.is_empty() {
            let active: std::collections::BTreeSet<_> =
                thresholds.iter().map(|(_, t)| *t).collect();
            self.raw_cache.retain(|k, _| active.contains(k));
            for &threshold in &active {
                if self.raw_cache.contains_key(&threshold) {
                    continue;
                }

                let mut raw: Vec<Stroke> = package
                    .manifest
                    .geometry
                    .par_iter()
                    .flat_map_iter(|p| {
                        let points: Vec<Point> = p
                            .points
                            .iter()
                            .map(|p| [p[0] as f32, p[1] as f32])
                            .collect();
                        assign_collections(&points, 0, package)
                    })
                    .collect();
                let groups: Vec<Vec<Stroke>> = package
                    .masks
                    .par_iter()
                    .enumerate()
                    .map(|(i, mask)| {
                        contours(
                            mask,
                            package.manifest.width as usize,
                            package.manifest.height as usize,
                            f32::from_bits(threshold),
                        )
                        .iter()
                        .flat_map(|p| assign_collections(p, i + 1, package))
                        .collect()
                    })
                    .collect();
                for group in groups {
                    raw.extend(group);
                }
                self.raw_cache.insert(threshold, raw);
            }
            self.raw = self
                .raw_cache
                .iter()
                .flat_map(|(&threshold, raw)| {
                    raw.iter()
                        .filter(move |p| {
                            layers
                                .get(&p.collection)
                                .map_or(settings.threshold, |l| l.settings.threshold)
                                .to_bits()
                                == threshold
                        })
                        .cloned()
                })
                .collect();
            self.thresholds = thresholds;
            self.base_settings = None;
        }
        let base_key = settings.reconstruction_key();
        let keys: BTreeMap<_, _> = layers
            .iter()
            .map(|(&id, l)| (id, l.settings.reconstruction_key()))
            .collect();
        if self.base_settings != Some(base_key) || self.layer_keys != keys {
            let mut buckets = BTreeMap::<(usize, u32), Vec<Vec<Point>>>::new();
            for s in &self.raw {
                buckets
                    .entry((s.group, s.collection))
                    .or_default()
                    .push(s.points.clone());
            }
            let outputs: Vec<_> = buckets
                .into_iter()
                .collect::<Vec<_>>()
                .into_par_iter()
                .map(|((group, collection), lines)| {
                    let settings = layers.get(&collection).map_or(settings, |l| l.settings);
                    let (lines, joins) = join(lines, if group == 0 { settings.gap } else { 0. });
                    let paths: Vec<Stroke> = lines
                        .into_iter()
                        .filter(|p| length(p) >= settings.minimum)
                        .map(|p| Stroke {
                            widths: Vec::new(),
                            points: simplify(&p, settings.tolerance),
                            group,
                            collection,
                        })
                        .collect();
                    (paths, joins)
                })
                .collect();
            self.base.clear();
            self.joins.clear();
            for (paths, joins) in outputs {
                self.base.extend(paths);
                self.joins.extend(joins);
            }
            self.base_settings = Some(base_key);
            self.layer_keys = keys;
        }
        let mut counts = BTreeMap::<(u32, usize), u32>::new();
        let indexed: Vec<_> = self
            .base
            .iter()
            .map(|p| {
                let count = counts.entry((p.collection, p.group)).or_default();
                let seed = count
                    .wrapping_mul(31)
                    .wrapping_add(p.collection.wrapping_mul(7919))
                    .wrapping_add(p.group as u32 * 397);
                *count += 1;
                (seed, p)
            })
            .collect();
        let paths = indexed
            .into_par_iter()
            .map(|(seed, p)| {
                let settings = layers.get(&p.collection).map_or(settings, |l| l.settings);
                let (points, widths) = vary(&p.points, settings, settings.seed.wrapping_add(seed));
                Stroke {
                    points,
                    widths,
                    group: p.group,
                    collection: p.collection,
                }
            })
            .collect();
        Processed {
            raw: self.raw.clone(),
            paths,
            joins: self.joins.clone(),
            milliseconds: started.elapsed().as_secs_f64() * 1000.,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn combined_variation_is_bounded_repeatable_and_seamless() {
        let settings = Settings {
            wobble: 2.,
            secondary: 1.,
            secondary_wavelength: 8.,
            size_jitter: 70.,
            size_wavelength: 11.,
            ..Default::default()
        };
        let line = [[0., 0.], [100., 0.]];
        let (points, widths) = vary(&line, settings, 42);
        assert_eq!((points.clone(), widths.clone()), vary(&line, settings, 42));
        assert_eq!(points.len(), widths.len());
        assert!(points.iter().all(|p| p[1].abs() <= 3.));
        assert!(widths.iter().all(|w| (0.3..=1.7).contains(w)));
        assert!(widths.windows(2).any(|w| w[0] != w[1]));
        assert_eq!(points[0], line[0]);
        assert_eq!(*points.last().unwrap(), line[1]);
        assert_ne!(
            points,
            vary(
                &line,
                Settings {
                    secondary: 0.,
                    ..settings
                },
                42
            )
            .0
        );
        let ring = [[0., 0.], [100., 0.], [100., 100.], [0., 0.]];
        let (points, widths) = vary(&ring, settings, 42);
        assert_eq!(points.first(), points.last());
        assert_eq!(widths.first(), widths.last());
        let (_, size_only) = vary(
            &line,
            Settings {
                size_jitter: 50.,
                ..Default::default()
            },
            1,
        );
        assert!(!size_only.is_empty());
    }
    #[test]
    fn layer_rules_and_noise_are_independent() {
        let mut package = crate::model::fixture();
        package.manifest.geometry = vec![
            Geometry {
                points: vec![[1., 5.], [8., 5.]],
            },
            Geometry {
                points: vec![[12., 5.], [19., 5.]],
            },
        ];
        let settings = Settings::subtle();
        let mut processor = Processor::default();
        let first = processor.process(&package, settings);
        let right = first.paths.iter().find(|p| p.collection == 2).unwrap();
        let mut layers = std::collections::HashMap::from([(
            1,
            Layer {
                color: Default::default(),
                settings: Settings {
                    minimum: 100.,
                    ..settings
                },
                styles: std::array::from_fn(|_| Style::default()),
            },
        )]);
        let changed = processor.process_layers(&package, settings, &layers);
        assert!(changed.paths.iter().all(|p| p.collection == 2));
        assert_eq!(changed.paths[0].points, right.points);
        assert_eq!(changed.paths[0].widths, right.widths);
        layers.get_mut(&1).unwrap().settings.minimum = 0.;
        layers.get_mut(&1).unwrap().settings.wobble = 4.;
        let changed = processor.process_layers(&package, settings, &layers);
        assert_eq!(
            changed
                .paths
                .iter()
                .find(|p| p.collection == 2)
                .unwrap()
                .points,
            right.points
        );
    }
    #[test]
    fn mask_thresholds_apply_only_to_their_layer() {
        let mut package = crate::model::fixture();
        for y in 2..8 {
            for x in 2..8 {
                package.masks[0][y * 20 + x] = 0.6;
                package.masks[0][y * 20 + x + 10] = 0.6;
            }
        }
        let layers = std::collections::HashMap::from([(
            1,
            Layer {
                color: Default::default(),
                settings: Settings {
                    threshold: 0.8,
                    ..Default::default()
                },
                styles: std::array::from_fn(|_| Style::default()),
            },
        )]);
        let mut processor = Processor::default();
        let result = processor.process_layers(&package, Settings::default(), &layers);
        assert!(!result.paths.is_empty());
        assert!(result.paths.iter().all(|p| p.collection == 2));
        let restored = processor.process_layers(
            &package,
            Settings::default(),
            &std::collections::HashMap::new(),
        );
        assert!(restored.paths.iter().any(|p| p.collection == 1));
        assert!(restored.paths.iter().any(|p| p.collection == 2));
    }
    #[test]
    fn joins_only_aligned() {
        let (paths, bridges) = join(
            vec![
                vec![[0., 0.], [5., 0.]],
                vec![[6., 0.], [10., 0.]],
                vec![[0., 1.], [5., 1.]],
            ],
            2.,
        );
        assert_eq!(paths.len(), 2);
        assert_eq!(bridges.len(), 1);
    }
    #[test]
    fn contours_keep_holes() {
        let mut m = vec![0.; 30 * 30];
        for y in 3..27 {
            for x in 3..27 {
                m[y * 30 + x] = 1.;
            }
        }
        for y in 10..20 {
            for x in 10..20 {
                m[y * 30 + x] = 0.;
            }
        }
        let c = contours(&m, 30, 30, 0.5);
        assert_eq!(c.len(), 2);
        for p in c {
            assert_eq!(p[0], *p.last().unwrap());
            let s = simplify(&p, 0.6);
            assert_eq!(s[0], *s.last().unwrap());
            assert!(s.len() >= 4);
        }
    }
    #[test]
    fn wobble_seed_endpoints_and_bounds() {
        let line = vec![[0., 0.], [100., 0.]];
        let p = wobble(&line, 3., 20., 7);
        assert_eq!(p, wobble(&line, 3., 20., 7));
        assert_ne!(p, wobble(&line, 3., 20., 8));
        assert_eq!(p[0], line[0]);
        assert_eq!(*p.last().unwrap(), line[1]);
        assert!(p.iter().all(|p| p[1].abs() <= 3.));
        assert_eq!(wobble(&line, 0., 20., 7), line);
    }
    #[test]
    fn wobble_closed_seam() {
        let p = vec![[0., 0.], [10., 0.], [10., 10.], [0., 10.], [0., 0.]];
        let w = wobble(&p, 1., 7., 1);
        assert_eq!(w[0], *w.last().unwrap());
    }
    #[test]
    fn reconstruction_does_not_join_collections() {
        let mut package = crate::model::fixture();
        package.manifest.geometry = vec![
            Geometry {
                points: vec![[1., 5.], [9., 5.]],
            },
            Geometry {
                points: vec![[11., 5.], [19., 5.]],
            },
        ];
        let output = Processor::default().process(
            &package,
            Settings {
                gap: 4.,
                ..Default::default()
            },
        );
        assert_eq!(output.paths.len(), 2);
        assert!(output.joins.is_empty());
        assert_eq!(output.paths[0].collection, 1);
        assert_eq!(output.paths[1].collection, 2);
        let split = assign_collections(&[[1., 5.], [19., 5.]], 0, &package);
        assert_eq!(split.len(), 2);
        assert_eq!(split[0].points.last(), split[1].points.first());
    }
    #[test]
    fn cache_preserves_repeatability() {
        let mut package = crate::model::fixture();
        package.manifest.geometry = vec![Geometry {
            points: vec![[1., 5.], [9., 5.]],
        }];
        let s = Settings {
            wobble: 2.,
            ..Default::default()
        };
        let mut processor = Processor::default();
        let a = processor.process(&package, s);
        let b = processor.process(&package, s);
        assert_eq!(a.paths[0].points, b.paths[0].points);
    }
}
