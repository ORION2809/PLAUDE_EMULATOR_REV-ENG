package com.plaudharness.speechbench

import org.json.JSONObject

data class Word(val w: String, val start: Double, val end: Double, val speaker: Int)
data class Turn(val start: Double, val end: Double, val speaker: Int)
data class Line(val speaker: String, val start: Double, val end: Double, val text: String)

object Transcript {
    fun words(result: JSONObject): List<Word> {
        val a = result.getJSONArray("words")
        return (0 until a.length()).map {
            val o = a.getJSONObject(it)
            Word(o.getString("w"), o.getDouble("start"), o.getDouble("end"), o.optInt("speaker", 0))
        }
    }

    fun turns(result: JSONObject): List<Turn> {
        val a = result.getJSONArray("turns")
        return (0 until a.length()).map {
            val o = a.getJSONObject(it)
            Turn(o.getDouble("start"), o.getDouble("end"), o.getInt("speaker"))
        }
    }

    /**
     * The harness's word-to-speaker rule (pipeline/base.py assign_speakers, tie-break
     * "floor"): a word takes the speaker with the largest total overlap; ties go to
     * the speaker of the earliest-starting overlapping turn; a word overlapping no
     * turn takes the nearest turn's speaker.
     */
    fun assign(words: List<Word>, turns: List<Turn>): List<Word> {
        if (turns.isEmpty()) return words.map { it.copy(speaker = 0) }
        val sorted = turns.sortedWith(compareBy({ it.start }, { it.end }))
        return words.map { w ->
            val overlap = HashMap<Int, Double>()
            val firstStart = HashMap<Int, Double>()
            for (t in sorted) {
                val o = minOf(w.end, t.end) - maxOf(w.start, t.start)
                if (o > 0) {
                    overlap[t.speaker] = (overlap[t.speaker] ?: 0.0) + o
                    if (t.speaker !in firstStart) firstStart[t.speaker] = t.start
                }
            }
            val speaker = if (overlap.isNotEmpty()) {
                val best = overlap.values.maxOrNull()!!
                overlap.filter { it.value >= best - 1e-9 }.keys.minByOrNull { firstStart[it]!! }!!
            } else {
                sorted.minByOrNull { t -> maxOf(t.start - w.end, w.start - t.end, 0.0) }!!.speaker
            }
            w.copy(speaker = speaker)
        }
    }

    /** Consecutive words of one speaker, split at pauses longer than [gapS]. */
    fun lines(words: List<Word>, gapS: Double = 1.0): List<Line> {
        val out = ArrayList<Line>()
        var cur = ArrayList<Word>()
        fun flush() {
            if (cur.isNotEmpty()) {
                out.add(Line(label(cur[0].speaker), cur.first().start, cur.maxOf { it.end }, cur.joinToString(" ") { it.w }))
                cur = ArrayList()
            }
        }
        for (w in words) {
            if (cur.isNotEmpty() && (w.speaker != cur.last().speaker || w.start - cur.last().end > gapS)) flush()
            cur.add(w)
        }
        flush()
        return out
    }

    private fun label(s: Int) = if (s > 0) "Speaker $s" else "Speaker ?"

    fun render(lines: List<Line>): String = lines.joinToString("\n") {
        "[%s %s] %s".format(clock(it.start), it.speaker, it.text)
    }

    private fun clock(s: Double): String {
        val t = s.toInt()
        return "%d:%02d".format(t / 60, t % 60)
    }
}
