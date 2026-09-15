//! Compact machine navigation with a window that always contains the selection.
use unicode_width::UnicodeWidthStr;

pub struct Tab {
    pub index: usize,
    pub text: String,
    pub x: u16,
    pub width: u16,
}

pub fn clipped(text: &str, width: usize) -> String {
    if text.width() <= width {
        return text.to_owned();
    }
    if width == 0 {
        return String::new();
    }
    let mut result = String::new();
    let mut used = 0;
    for c in text.chars() {
        let n = unicode_width::UnicodeWidthChar::width(c).unwrap_or(0);
        if used + n >= width {
            break;
        }
        result.push(c);
        used += n;
    }
    result.push('…');
    result
}

pub fn tabs(labels: &[String], selected: usize, width: u16) -> Vec<Tab> {
    if labels.is_empty() || selected >= labels.len() || width < 5 {
        return vec![];
    }
    let available = usize::from(width - 4);
    let cap = available.min(24);
    let texts: Vec<_> = labels
        .iter()
        .map(|label| {
            if cap < 2 {
                clipped(label, cap)
            } else {
                format!(" {} ", clipped(label, cap.saturating_sub(2)))
            }
        })
        .collect();
    let widths: Vec<_> = texts.iter().map(|s| s.width()).collect();
    let (mut first, mut last) = (selected, selected);
    let mut used = widths[selected];
    loop {
        let mut changed = false;
        if last + 1 < labels.len() && used + 1 + widths[last + 1] <= available {
            last += 1;
            used += 1 + widths[last];
            changed = true;
        }
        if first > 0 && used + 1 + widths[first - 1] <= available {
            first -= 1;
            used += 1 + widths[first];
            changed = true;
        }
        if !changed {
            break;
        }
    }
    let mut x = 2;
    (first..=last)
        .map(|index| {
            let width = widths[index] as u16;
            let tab = Tab {
                index,
                text: texts[index].clone(),
                x,
                width,
            };
            x += width + 1;
            tab
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn selection_always_visible_and_bounds_respected() {
        let labels = (0..30)
            .map(|i| format!("machine-{i}-界界-long-name"))
            .collect::<Vec<_>>();
        for width in [5, 12, 40, 60, 100] {
            for selected in 0..labels.len() {
                let layout = tabs(&labels, selected, width);
                assert!(layout.iter().any(|t| t.index == selected));
                assert!(layout.iter().all(|t| t.x + t.width <= width));
            }
        }
    }
    #[test]
    fn room_shows_neighbor_destinations() {
        let labels = vec!["alpha".into(), "beta".into(), "gamma".into()];
        let layout = tabs(&labels, 1, 40);
        assert_eq!(
            layout.iter().map(|t| t.index).collect::<Vec<_>>(),
            vec![0, 1, 2]
        );
    }
}
