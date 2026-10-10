module c {
  import a from "./a"
  var y: int
  action init = all { a::init, y' = 0 }
  action onlyY = all { a::x' = a::x, y' = y + 1 }
  action step = onlyY
  run t1 = init.then(onlyY).expect(a::x == 0 and y == 1)
}
