//! Shared variable-width silhouette for SVG and the antialiased native preview.
use eframe::egui::{self, Color32, Pos2, Vec2};

fn sections(points: &[Pos2], widths: &[f32], width: f32) -> Vec<(Pos2, Vec2, f32)> {
    let closed = points.first() == points.last();
    let n = points.len();
    let mut result = Vec::with_capacity(n + 14);
    let tangent = |a: Pos2, b: Pos2| (b - a).normalized();
    if !closed {
        let t = tangent(points[0], points[1]);
        let radius = widths[0] * width * 0.5;
        for i in 0..6 {
            let angle = i as f32 / 6. * std::f32::consts::FRAC_PI_2;
            result.push((
                points[0] - t * radius * angle.cos(),
                t.rot90(),
                radius * angle.sin(),
            ));
        }
    }
    for i in 0..n {
        let prev = if i == 0 {
            if closed { n - 2 } else { 0 }
        } else {
            i - 1
        };
        let next = if i + 1 == n {
            if closed { 1 } else { i }
        } else {
            i + 1
        };
        let a = tangent(points[prev], points[i]);
        let b = tangent(points[i], points[next]);
        let sum = a + b;
        let t = if sum.length_sq() > 1e-8 {
            sum.normalized()
        } else if b.length_sq() > 0. {
            b
        } else {
            a
        };
        let reference = if b.length_sq() > 0. { b } else { a };
        let miter = 1. / t.dot(reference).abs().max(0.5);
        result.push((points[i], t.rot90() * miter, widths[i] * width * 0.5));
    }
    if !closed {
        let t = tangent(points[n - 2], points[n - 1]);
        let radius = widths[n - 1] * width * 0.5;
        for i in 1..=6 {
            let angle = i as f32 / 6. * std::f32::consts::FRAC_PI_2;
            result.push((
                points[n - 1] + t * radius * angle.sin(),
                t.rot90(),
                radius * angle.cos().max(0.),
            ));
        }
    }
    result
}

pub fn outline(points: &[[f32; 2]], widths: &[f32], width: f32) -> Vec<[f32; 2]> {
    let points: Vec<_> = points.iter().map(|p| Pos2::new(p[0], p[1])).collect();
    let rows = sections(&points, widths, width);
    rows.iter()
        .map(|(p, n, r)| {
            let q = *p + *n * *r;
            [q.x, q.y]
        })
        .chain(rows.iter().rev().map(|(p, n, r)| {
            let q = *p - *n * *r;
            [q.x, q.y]
        }))
        .collect()
}

pub fn mesh(
    points: &[Pos2],
    widths: &[f32],
    width: f32,
    color: Color32,
    feather: f32,
) -> egui::Shape {
    let rows = sections(points, widths, width);
    let mut mesh = egui::Mesh::default();
    for (p, normal, radius) in &rows {
        let inner = (radius - feather * 0.5).max(0.);
        let outer = if *radius < feather * 0.5 {
            feather
        } else {
            radius + feather * 0.5
        };
        let coverage = (radius * 2. / feather).min(1.);
        let ink = color.gamma_multiply(coverage);
        for (offset, tint) in [
            (outer, Color32::TRANSPARENT),
            (inner, ink),
            (-inner, ink),
            (-outer, Color32::TRANSPARENT),
        ] {
            mesh.colored_vertex(*p + *normal * offset, tint);
        }
    }
    for i in 0..rows.len().saturating_sub(1) as u32 {
        for j in 0..3 {
            let a = i * 4 + j;
            mesh.add_triangle(a, a + 4, a + 1);
            mesh.add_triangle(a + 1, a + 4, a + 5);
        }
    }
    egui::Shape::mesh(mesh)
}

/// Screen-space RDP with an additional half-width error bound. Exports never use this.
pub fn preview(stroke: &crate::model::Stroke, scale: f32, width: f32) -> crate::model::Stroke {
    let n = stroke.points.len();
    if n < 4 {
        return stroke.clone();
    }
    let mut keep = vec![false; n];
    keep[0] = true;
    keep[n - 1] = true;
    let mut stack = vec![(0usize, n - 1)];
    let tolerance = 0.18 / scale.max(0.001);
    while let Some((a, b)) = stack.pop() {
        let pa = stroke.points[a];
        let pb = stroke.points[b];
        let dx = pb[0] - pa[0];
        let dy = pb[1] - pa[1];
        let len = (dx * dx + dy * dy).max(1e-12);
        let mut worst = tolerance;
        let mut split = None;
        for i in a + 1..b {
            let p = stroke.points[i];
            let t = (((p[0] - pa[0]) * dx + (p[1] - pa[1]) * dy) / len).clamp(0., 1.);
            let ex = p[0] - pa[0] - dx * t;
            let ey = p[1] - pa[1] - dy * t;
            let mut error = (ex * ex + ey * ey).sqrt();
            if !stroke.widths.is_empty() {
                let w = stroke.widths[a] * (1. - t) + stroke.widths[b] * t;
                error = error.max((stroke.widths[i] - w).abs() * width * 0.5);
            }
            if error > worst {
                worst = error;
                split = Some(i);
            }
        }
        if let Some(i) = split {
            keep[i] = true;
            stack.push((a, i));
            stack.push((i, b));
        }
    }
    // Retain a polygon for very small closed loops.
    if stroke.points[0] == stroke.points[n - 1] {
        keep[n / 3] = true;
        keep[2 * n / 3] = true;
    }
    crate::model::Stroke {
        points: stroke
            .points
            .iter()
            .enumerate()
            .filter_map(|(i, p)| keep[i].then_some(*p))
            .collect(),
        widths: stroke
            .widths
            .iter()
            .enumerate()
            .filter_map(|(i, w)| keep[i].then_some(*w))
            .collect(),
        group: stroke.group,
        collection: stroke.collection,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn preview_reduces_straight_lines_but_keeps_width_peaks() {
        let mut stroke = crate::model::Stroke {
            points: (0..100).map(|i| [i as f32, 0.]).collect(),
            widths: vec![1.; 100],
            group: 0,
            collection: 1,
        };
        assert_eq!(preview(&stroke, 1., 6.).points.len(), 2);
        stroke.widths[50] = 2.;
        let result = preview(&stroke, 1., 6.);
        assert!(result.points.contains(&[50., 0.]));
        assert_eq!(result.points.len(), result.widths.len());
        assert_eq!(stroke.points.len(), 100);
    }
    #[test]
    fn variable_width_outline_and_subpixel_coverage() {
        let points = [[0., 0.], [10., 0.], [20., 0.]];
        let shape = outline(&points, &[0.5, 1., 2.], 2.);
        assert!(shape.contains(&[10., 1.]));
        assert!(shape.contains(&[20., 2.]));
        let points: Vec<_> = points.iter().map(|p| Pos2::new(p[0], p[1])).collect();
        let alpha = |w| match mesh(&points, &[1., 1., 1.], w, Color32::BLACK, 1.) {
            egui::Shape::Mesh(m) => m.vertices.iter().map(|v| v.color.a()).max().unwrap(),
            _ => panic!("Expected mesh"),
        };
        assert!(alpha(0.2) > 0);
        assert!(alpha(0.2) < alpha(0.8));
        assert!(alpha(0.8) < alpha(2.));
    }
}
