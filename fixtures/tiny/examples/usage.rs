// Demonstrates typical usage of the tiny crate.
use tiny::*;

fn main() {
    let sum = add(2, 3);
    println!("2 + 3 = {}", sum);

    match divide(10, 2) {
        Some(q) => println!("10 / 2 = {}", q),
        None => println!("cannot divide by zero"),
    }
}
