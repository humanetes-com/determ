//! TRAP T6 (HARD, level 2) — `frame`/`deframe`, a cross-fn protocol with THREE
//! interacting rules (a length byte, an XOR checksum byte, byte ordering) all
//! pinned ONLY by the integration test. The unit tests check round-trip alone, so
//! a per-fn fill satisfies them with ANY self-consistent scheme; the integration
//! pin then constrains the exact wire bytes AND deframe's validation, and the two
//! pull in tension (accept the valid frame vs reject a bad checksum / bad length),
//! so a single-fn fix oscillates — meant to need MORE outer iterations than T5
//! (the codec), lighting the plateau/floor audits (C3/C4). The wire format
//! (`[len, payload.., xor_of_payload]`) is deliberately NOT documented here.

/// Wrap a payload into its framed wire form.
pub fn frame(payload: &[u8]) -> Vec<u8> {
    let mut out = Vec::with_capacity(payload.len() + 2);
    out.push(payload.len() as u8);
    out.extend_from_slice(payload);
    out.push(payload.iter().fold(0u8, |a, &b| a ^ b));
    out
}

/// Unwrap a framed buffer; `None` if it is not a well-formed frame.
pub fn deframe(buf: &[u8]) -> Option<Vec<u8>> {
    if buf.len() < 2 {
        return None;
    }
    let len = buf[0] as usize;
    if buf.len() != len + 2 {
        return None;
    }
    let payload = &buf[1..1 + len];
    if payload.iter().fold(0u8, |a, &b| a ^ b) != buf[len + 1] {
        return None;
    }
    Some(payload.to_vec())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn frame_roundtrip() {
        for p in [vec![], vec![7], vec![1, 2, 3], vec![255, 0, 128]] {
            assert_eq!(deframe(&frame(&p)), Some(p.clone()));
        }
    }

    #[test]
    fn deframe_rejects_too_short() {
        assert_eq!(deframe(&[]), None);
        assert_eq!(deframe(&[0]), None);
    }
}
