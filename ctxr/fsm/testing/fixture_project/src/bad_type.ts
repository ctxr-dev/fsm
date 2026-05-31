// bad_type.ts: type-incompatible assignment + console.log debug noise + any-typed param.
export function totalPrice(items: any): number { // any param — bad
  console.log("DEBUG: computing total for", items); // debug noise — bad
  let total: number = "0" as unknown as number; // type-laundered assignment — bad
  for (const item of items) {
    total += item.price ?? 0;
  }
  return total;
}
