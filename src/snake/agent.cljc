(ns snake.agent
  (:require
    [snake.model :as model]))

(def dead-score 0)

(defn score
  [{:keys [body food dead?]}]
  (if dead?
    dead-score
    (let [distance
          (let [x (- (first (last body)) (first food))
                y (- (last (last body)) (last food))]
            (+ (Math/abs y) (Math/abs x)))]
      (+ (* 10 (count body)) (/ 1 distance)))))

(def actions [:up :down :left :right])

(def look-ahead 35)

(defn best-solution
  [candidates]
  (apply max-key (comp score :state) candidates))

(defn best-action
  [state]
  (loop [candidates [{:state state :actions []}]
         iteration 0]
    (if (< look-ahead iteration)
      (->> candidates
           (filter (comp not-empty :actions))
           (best-solution)
           :actions
           (first))
      (let [best (best-solution candidates)]
        (recur
          (into candidates
                (->> actions
                     (map (fn [action]
                            {:state (model/tick (:state best) action)
                             :actions (conj (:actions best) action)}))
                     (filter #(not= dead-score (score (:state %))))))
          (inc iteration))))))
