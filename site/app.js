const commands = [
  'run "Build a production-ready FastAPI service with tests."',
  "doctor --config localforge.yaml",
  "show-run 20260702-053717 --tail 20 --json",
  "path-info projects/api --json",
];

const logEvents = [
  ["00:00:01", "run_start", "workspace=/Users/dima/aimodel_playground"],
  ["00:00:03", "mcp_ready", "filesystem: 14 tools registered"],
  ["00:00:06", "model_wait", "ollama model generation in progress"],
  ["00:00:18", "tool_call", "list_files path=. max_files=100"],
  ["00:00:19", "tool_result", "list_files ok"],
  ["00:00:31", "tool_call", "write_file projects/api/main.py"],
  ["00:00:32", "verification_pending", "mutation requires proof before final"],
  ["00:00:41", "tool_call", "python -m unittest discover -s tests"],
  ["00:00:43", "verification_satisfied", "tests passed"],
  ["00:00:45", "run_final", "accepted final report"],
];

const typedCommand = document.querySelector("#typed-command");
let commandIndex = 0;
let charIndex = 0;
let deleting = false;

function typeCommand() {
  if (!typedCommand) return;
  const command = commands[commandIndex];
  typedCommand.textContent = command.slice(0, charIndex);

  if (!deleting && charIndex < command.length) {
    charIndex += 1;
    window.setTimeout(typeCommand, 42);
    return;
  }

  if (!deleting && charIndex === command.length) {
    deleting = true;
    window.setTimeout(typeCommand, 1500);
    return;
  }

  if (deleting && charIndex > 0) {
    charIndex -= 1;
    window.setTimeout(typeCommand, 18);
    return;
  }

  deleting = false;
  commandIndex = (commandIndex + 1) % commands.length;
  window.setTimeout(typeCommand, 250);
}

function renderLogStream() {
  const stream = document.querySelector("#log-stream");
  if (!stream) return;
  let index = 0;

  function appendLine() {
    const [time, type, message] = logEvents[index];
    const line = document.createElement("div");
    line.className = "log-line";
    line.innerHTML = `<span>${time}</span><span>${type}</span><span>${message}</span>`;
    stream.appendChild(line);

    while (stream.children.length > 8) {
      stream.removeChild(stream.firstElementChild);
    }

    index = (index + 1) % logEvents.length;
    window.setTimeout(appendLine, index === 0 ? 1400 : 760);
  }

  appendLine();
}

function revealCards() {
  const cards = [...document.querySelectorAll(".reveal")];
  const observer = new IntersectionObserver(
    (entries) => {
      entries.forEach((entry) => {
        if (entry.isIntersecting) {
          entry.target.classList.add("visible");
          observer.unobserve(entry.target);
        }
      });
    },
    { threshold: 0.2 }
  );

  cards.forEach((card) => observer.observe(card));
}

function animateAuditLane() {
  const events = [...document.querySelectorAll(".audit-event")];
  if (!events.length) return;
  let index = 0;
  window.setInterval(() => {
    events.forEach((event) => event.classList.remove("active"));
    events[index].classList.add("active");
    index = (index + 1) % events.length;
  }, 1300);
}

function addTerminalTilt() {
  const terminal = document.querySelector("[data-tilt]");
  if (!terminal) return;

  terminal.addEventListener("pointermove", (event) => {
    const rect = terminal.getBoundingClientRect();
    const x = (event.clientX - rect.left) / rect.width - 0.5;
    const y = (event.clientY - rect.top) / rect.height - 0.5;
    terminal.style.transform = `rotateX(${y * -3}deg) rotateY(${x * 4}deg)`;
  });

  terminal.addEventListener("pointerleave", () => {
    terminal.style.transform = "rotateX(0deg) rotateY(0deg)";
  });
}

function runSignalCanvas() {
  const canvas = document.querySelector("#signal-canvas");
  if (!canvas || window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  const context = canvas.getContext("2d");
  const nodes = [];
  const nodeCount = 72;
  let width = 0;
  let height = 0;
  let frame = 0;

  function resize() {
    const ratio = window.devicePixelRatio || 1;
    width = window.innerWidth;
    height = window.innerHeight;
    canvas.width = Math.floor(width * ratio);
    canvas.height = Math.floor(height * ratio);
    canvas.style.width = `${width}px`;
    canvas.style.height = `${height}px`;
    context.setTransform(ratio, 0, 0, ratio, 0, 0);
  }

  function seed() {
    nodes.length = 0;
    for (let i = 0; i < nodeCount; i += 1) {
      nodes.push({
        x: Math.random() * width,
        y: Math.random() * height,
        vx: (Math.random() - 0.5) * 0.34,
        vy: (Math.random() - 0.5) * 0.34,
        pulse: Math.random() * Math.PI * 2,
      });
    }
  }

  function draw() {
    frame += 1;
    context.clearRect(0, 0, width, height);
    context.lineWidth = 1;

    nodes.forEach((node, i) => {
      node.x += node.vx;
      node.y += node.vy;
      node.pulse += 0.025;

      if (node.x < -20) node.x = width + 20;
      if (node.x > width + 20) node.x = -20;
      if (node.y < -20) node.y = height + 20;
      if (node.y > height + 20) node.y = -20;

      for (let j = i + 1; j < nodes.length; j += 1) {
        const other = nodes[j];
        const dx = node.x - other.x;
        const dy = node.y - other.y;
        const distance = Math.hypot(dx, dy);
        if (distance < 155) {
          const alpha = (1 - distance / 155) * 0.22;
          context.strokeStyle = `rgba(114, 255, 173, ${alpha})`;
          context.beginPath();
          context.moveTo(node.x, node.y);
          context.lineTo(other.x, other.y);
          context.stroke();
        }
      }

      const radius = 1.4 + Math.sin(node.pulse) * 0.8;
      context.fillStyle = i % 5 === frame % 5 ? "rgba(113, 227, 255, 0.85)" : "rgba(114, 255, 173, 0.72)";
      context.beginPath();
      context.arc(node.x, node.y, radius, 0, Math.PI * 2);
      context.fill();
    });

    window.requestAnimationFrame(draw);
  }

  resize();
  seed();
  draw();
  window.addEventListener("resize", () => {
    resize();
    seed();
  });
}

typeCommand();
renderLogStream();
revealCards();
animateAuditLane();
addTerminalTilt();
runSignalCanvas();
