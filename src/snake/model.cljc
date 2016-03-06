(ns snake.model)

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

(defn move
  [body direction]
  (-> body
      (conj (new-head (last body) direction))
      (subvec 1)))

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;; API
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;

(defn tick
  [{:keys [direction body] :as old-state} input]
  (let [new-direction (if (some? input)
                        (new-direction direction input)
                        direction)
        new-body (move body new-direction)]
    (merge
      old-state
      {:direction new-direction
       :body      new-body})))

(defn init
  [params]
  (merge params {:body      [[0 0] [0 1] [0 2] [0 3]]
                 :direction [0 1]}))

(defn render
  [{:keys [width height body]}]
  (let [body (set body)
        layout
        (map
          (fn [x]
            (map
              (fn [y]
                (let [color (cond
                              (contains? body [x y]) :tomato
                              :else :black)]
                  {:color color}))
              (range height)))
          (range width))]
    layout))
