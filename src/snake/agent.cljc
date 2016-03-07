(ns snake.agent
  (:require
    [snake.model :as model]))

(defn score
  [{:keys [body food dead?]}]
  (if dead?
    0
    (let [length (count body)
          distance (let [x (- (first (last body)) (first food))
                         y (- (last (last body)) (last food))]
                     (+ (* y y) (* x x)))]
      (+ (* 10 length) (/ 1 distance)))))

(def actions [:up :down :left :right])

(defn gen-actions
  [state path depth]
  (if (= depth (count path))
    [path]
    (mapcat
      (fn [action]
        (gen-actions
          (model/tick state action)
          (conj path action)
          depth))
      actions)))

(defn score-actions
  [state actions]
  (score (reduce model/tick state actions)))

(defn best-action
  [state]
  (let [all-actions (gen-actions state [] 2)
        best-set (apply max-key (partial score-actions state) all-actions)]
    (first best-set)))