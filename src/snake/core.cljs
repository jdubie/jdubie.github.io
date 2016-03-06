(ns snake.core
  (:require
    [snake.model :as model]
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
          #_(println "coords" x y (+ x size) (+ y size))
          (.fillRect ctx x y (+ x size) (+ y size)))))))

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
  (swap! state-atom
         (fn [{:keys [state input]}]
           {:state (model/tick state input)}))
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
    (js/setInterval #(tick! state) 500)))

(.addEventListener js/document "DOMContentLoaded" run false)
