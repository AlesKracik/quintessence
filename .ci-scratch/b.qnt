module b {
  import a from "./a"
  var y: int
  action init = all { a::init, y' = 0 }
  action bump = all { a::inc, y' = y + 1 }
  action onlyY = all { a::stay, y' = y + 1 }
  action capped = all { y < 1, a::stay, y' = y + 1 }
  action step = any { bump, onlyY }
  run t1 = init.then(bump).expect(a::x == 1 and y == 1)
  run t2 = init.then(onlyY).expect(a::x == 0 and y == 1)
  run t3 = init.then(bump).expect(y == 1).then(bump).expect(y == 2)
  run t4 = init.then(capped).expect(y == 1).then(capped.fail())
  val inv = y >= 0
}
