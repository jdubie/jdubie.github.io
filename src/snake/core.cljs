(ns snake.core
  (:require
    [snake.model :as model]
    [snake.agent :as agent]
    [goog.dom :as gdom]))

(enable-console-print!)

(def canvas-id "canvas-id")

(defn remove-canvas! []
  (gdom/removeNode (gdom/getElement canvas-id)))

(defn ensure-canvas! []
  (when-not (gdom/getElement canvas-id)
    (.appendChild
      (.-body js/document)
      (doto (.createElement js/document "canvas")
        (.setAttribute "width" js/window.innerWidth)
        (.setAttribute "height" js/window.innerHeight)
        (.setAttribute "id" canvas-id))))
  (gdom/getElement canvas-id))

(def size 40)

(defn render
  [grid]
  (let [canvas (ensure-canvas!)
        ctx (.getContext canvas "2d")
        grid-with-coords
        (map-indexed
          (fn [x row]
            (map-indexed
              (fn [y value]
                (merge {:x x :y y} value))
              row))
          grid)]
    (.clearRect
      ctx 0 0
      (* size (count grid))
      (* size (count (first grid))))
    (doseq [row grid-with-coords]
      (doseq [{:keys [x y color]} row]
        (let [x (* size x)
              y (* size y)]
          (aset ctx "fillStyle" (name color))
          (.fillRect ctx x y (+ x size) (+ y size)))))
    (let [width (.-width canvas)
          height (.-height canvas)
          start-width (- width (mod width size))
          start-height (- height (mod height size))]
      (aset ctx "fillStyle" "black")
      (.fillRect ctx start-width 0 width height)
      (.fillRect ctx 0 start-height width height))))

(defn screen []
  (let [canvas (ensure-canvas!)
        [width height] (map #(Math/floor (/ % size))
                            [(.-width canvas) (.-height canvas)])]
    {:width width :height height}))

(defn init
  []
  (atom {:state (model/init (screen))}))

(defn tick!
  [state-atom]
  ;; agent plays the game
  (swap! state-atom assoc :input
          (agent/best-action (:state @state-atom)))

  ;; auto respawn
  (swap! state-atom (fn [{:keys [state] :as a}]
                      (if (:dead? state)
                        {:state (model/init state)}
                        a)))

  ;; move model forward based on user input
  (swap! state-atom
         (fn [{:keys [state input]}]
           {:state (model/tick state input)}))

  ;; render current state
  (let [{:keys [state]} @state-atom]
    (render (model/render state))))

(defn keycode->left-right
  [code]
  (case code
    (38 87) :up
    (40 83) :down
    (37 65) :left
    (39 68) :right
    nil))

(defn run
  []
  (let [state (init)]
    (.addEventListener
      js/document "keydown"
      (fn [e]
        (swap! state assoc :input (keycode->left-right (.-keyCode e)))))
    (tick! state)
    (js/setInterval #(tick! state) 0)))

(.addEventListener js/document "DOMContentLoaded" run false)
