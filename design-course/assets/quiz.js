// design-course/assets/quiz.js — 可复用选择题组件（所有课程共用，勿内联进 lesson）。
//
// 用法：
//   <div class="quiz" data-explain="为什么正确答案正确（以及错误选项错在哪）">
//     <p class="quiz-q">题干…？</p>
//     <button class="quiz-option" data-answer="right">正确项</button>   // 只放一个 right
//     <button class="quiz-option">干扰项 A</button>
//     <button class="quiz-option">干扰项 B</button>
//     <p class="quiz-feedback" aria-live="polite"></p>
//   </div>
//
// 规则：选项文案长度应彼此接近，不通过排版泄露答案。
(function () {
  document.querySelectorAll('.quiz[data-explain]').forEach(function (quiz) {
    var feedback = quiz.querySelector('.quiz-feedback');
    var options = Array.prototype.slice.call(quiz.querySelectorAll('.quiz-option'));
    options.forEach(function (btn) {
      btn.addEventListener('click', function () {
        if (quiz.dataset.done) return;
        quiz.dataset.done = '1';
        var right = btn.dataset.answer === 'right';
        options.forEach(function (o) {
          o.disabled = true;
          if (o.dataset.answer === 'right') o.classList.add('is-right');
        });
        if (!right) btn.classList.add('is-wrong');
        feedback.textContent = (right ? '答对了 —— ' : '再想想 —— ') + quiz.dataset.explain;
        feedback.classList.add(right ? 'is-right' : 'is-wrong');
      });
    });
  });
})();
