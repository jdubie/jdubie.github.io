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

(defn crashed-into-body?
  [{:keys [body]}]
  (not= (count body)
        (count (set body))))

(comment
  (crashed-into-body? {:body [[0 0] [1 0]]})
  (crashed-into-body? {:body [[0 0] [0 0]]}))

(defn inbounds?
  [{:keys [body width height]}]
  (every?
    (fn [[x y]]
      (and (< x width)
           (< y height)
           (>= x 0)
           (>= y 0)))
    body))

(comment
  (= true (inbounds? {:body [[0 0]] :width 1 :height 1}))
  (= false (inbounds? {:body [[-1 0]] :width 1 :height 1}))
  (= false (inbounds? {:body [[0 1]] :width 1 :height 1})))

(defn dead?
  [{:keys [body width height]}]
  (or (crashed-into-body? {:body body})
      (not (inbounds? {:body body :width width :height height}))))

(defn tick
  [{:keys [direction body food width height] :as old-state} input]
  (if (:dead? old-state)
    old-state
    (let [new-direction (if (some? input)
                          (new-direction direction input)
                          direction)
          move-ahead-body (conj body (new-head (last body) new-direction))
          ate-food (contains? (set move-ahead-body) food)
          new-food (if ate-food (rand-food old-state) food)
          new-body (if ate-food move-ahead-body (subvec move-ahead-body 1))]
      (merge
        old-state
        {:direction new-direction
         :dead? (dead? {:body new-body :width width :height height})
         :food new-food
         :body new-body}))))

(defn init
  [params]
  (merge params {:body [[0 0] [0 1] [0 2] [0 3] [0 4] [0 5] [0 6]]
                 :food (rand-food params)
                 :dead? false
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
