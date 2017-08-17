(ns snake.agent
  (:require
    [snake.model :as model]))

(def dead-score 0)

(defn score
  [{:keys [body food dead?]}]
  (if dead?
    dead-score
    (let [length (count body)
          distance (let [x (- (first (last body)) (first food))
                         y (- (last (last body)) (last food))]
                     (+ (Math/abs y) (Math/abs x)))]
      (+ (* 10 length) (/ 1 distance)))))

(def actions [:up :down :left :right])

(def look-ahead 10)

(defn best-action
  [state]
  (loop [candidates [{:state state :actions []}]
         iteration 0]
    (let [best (apply max-key (comp score :state) candidates)]
      (if
        (< look-ahead iteration) (-> best :actions (first))
        (recur
          (into candidates
                (->> actions
                     (map (fn [action]
                            {:state (model/tick (:state best) action)
                             :actions (conj (:actions best) action)}))
                     (filter #(not= dead-score (score (:state %))))))
          (inc iteration))))))