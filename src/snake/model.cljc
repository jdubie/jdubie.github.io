(ns snake.model)

(defn rand-food
  [{:keys [width height]}]
  [(rand-int width) (rand-int height)])

(defn new-direction
  [old input]
  "Old allow 90 degree turns"
  (let [proposed
        (case input
          :up [0 -1]
          :down [0 1]
          :left [-1 0]
          :right [1 0])]
    (if (= [1 1] (into [] (map (fn [x] (* x x))
                               (map + old proposed))))
      proposed
      old)))

(defn new-head
  [head direction]
  (into []
        (map + head direction)))

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;; API
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;

(defn tick
  [{:keys [direction body food dead? width height] :as old-state} input]
  (if dead?
    old-state
    (let [new-direction (if (some? input)
                          (new-direction direction input)
                          direction)
          move-ahead-body (conj body (new-head (last body) new-direction))
          ate-food (contains? (set move-ahead-body) food)
          new-food (if ate-food (rand-food old-state) food)
          new-body (if ate-food move-ahead-body (subvec move-ahead-body 1))
          dead? (or (not= (count new-body) (count (set new-body)))
                    (some neg? (flatten new-body))
                    (some (fn [x y] (or (> x width)
                                        (> y height))) new-body))]
      (merge
        old-state
        {:direction new-direction
         :dead?     dead?
         :food      new-food
         :body      new-body}))))

(defn init
  [params]
  (merge params {:body      [[0 0] [0 1] [0 2] [0 3] [0 4] [0 5] [0 6]]
                 :food      (rand-food params)
                 :dead?     false
                 :direction [0 1]}))

(defn render
  [{:keys [width height body food dead?]}]
  (let [body (set body)
        layout
        (map
          (fn [x]
            (map
              (fn [y]
                (let [color (cond
                              (contains? body [x y]) :tomato
                              (= food [x y]) :aqua
                              :else (if dead? :grey :black))]
                  {:color color}))
              (range height)))
          (range width))]
    layout))
